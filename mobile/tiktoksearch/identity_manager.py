"""Hot-reloadable warm-identity store with health tracking.

The direct-API path needs a WARM v46 identity (device_id + iid + cookie[sessionid]
+ x-tt-token + full device_query). These EXPIRE — when they lapse TikTok soft-blocks
the request (`hit_shark`): HTTP 200 + empty results (see memory:
intermittent-empty-200-diagnosis, direct-api-works).

This module decouples identities from the frozen ClientConfig so an external
refresher (the emulator/mitmproxy capture loop) can drop a fresh identities.json
on disk and have the running server pick it up WITHOUT a restart. It also tracks
per-identity health: consecutive empty ("shadow-block") results mark an identity
`stale` so the pool stops handing it out until it is refreshed.

Wire-up:
  * A capture process (emulator + app logged in + mitmproxy addon) writes the
    latest {device_id, iid, cookie, x_tt_token, device_query, ...} entries to
    `identities.json` whenever the app refreshes its session.
  * The pool builds one slot per identity via `IdentityStore.snapshot()` and, on
    every search, calls `report_ok` / `report_empty` so the store can retire a
    dead identity and surface a fresh one on the next `mtime` change.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger('tiktoksearch.identity')

# Consecutive empty/shadow-blocked results before an identity is retired.
DEFAULT_STALE_AFTER = 3
# The captured argus pair, named as one unit because that is what it is: the
# two halves are only ever carried, compared and rejected together.
DYN_PAIR_FIELDS: tuple[str, str] = ('dyn_seed', 'dyn_rand')


@dataclass
class Identity:
    """One warm device identity plus its live health state."""
    key: str
    device_id: str
    iid: str
    cookie: str | None = None
    x_tt_token: str | None = None
    user_agent: str | None = None
    device_query: Mapping[str, Any] = field(default_factory=dict)
    # The captured argus `(f24 dyn_seed, f3 rand)` pair. SECRET, same class as
    # `cookie`: never logged, never echoed, masked first6…last4 if a diagnostic
    # must name it. Optional — an identity without one is a perfectly good
    # identity and every existing path works unchanged; what is NOT a thing is
    # HALF a pair (see __post_init__).
    dyn_seed: str | None = None
    dyn_rand: int | None = None
    # health
    consecutive_empty: int = 0
    stale: bool = False
    last_ok: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Refuse a HALF pair, so no half-paired identity can exist.

        A `dyn_seed` with no `dyn_rand` is a configuration error, not a
        half-usable identity: a good f24 beside a freely chosen f3 was MEASURED
        to be refused with a zero-byte body, so a half pair is not "the pair,
        degraded" — it is a broken identity that looks configured. `IdentityStore`
        turns this into a loud ERROR and drops that ENTRY, never a silent
        downgrade to no-pair. The message names the identity key and neither
        half of the pair."""
        if bool(self.dyn_seed) != (self.dyn_rand is not None):
            have, missing = DYN_PAIR_FIELDS if self.dyn_seed else DYN_PAIR_FIELDS[::-1]
            raise ValueError(f'identity {self.key}: {have} is set without {missing} — the captured argus pair is one unit, carry both or neither')

    def overrides(self) -> dict[str, Any]:
        """Fields to feed into ClientConfig.with_overrides for this identity."""
        out: dict[str, Any] = {'device_id': self.device_id, 'iid': self.iid}
        if self.cookie:
            out['cookie'] = self.cookie
        if self.x_tt_token:
            out['x_tt_token'] = self.x_tt_token
        if self.user_agent:
            out['user_agent'] = self.user_agent
        if self.device_query:
            out['device_query'] = dict(self.device_query)
        if self.dyn_seed:
            # Both, always: __post_init__ has already proven they travel
            # together, and `ClientConfig` would reject a half pair anyway.
            out['dyn_seed'] = self.dyn_seed
            out['dyn_rand'] = self.dyn_rand
        return out

    def has_dyn_pair(self) -> bool:
        """Whether this identity can sign the user-scoped endpoints."""
        return bool(self.dyn_seed)

    def report_ok(self) -> None:
        with self._lock:
            self.consecutive_empty = 0
            self.stale = False
            self.last_ok = time.time()

    def report_empty(self, stale_after: int) -> bool:
        """Record a shadow-blocked (empty) result. Returns True if this tipped
        the identity into `stale`."""
        with self._lock:
            self.consecutive_empty += 1
            if not self.stale and self.consecutive_empty >= stale_after:
                self.stale = True
                logger.warning(
                    'identity %s marked STALE after %d consecutive empty results '
                    '(cookie/x-tt-token likely expired — needs refresh)',
                    self.key, self.consecutive_empty)
                return True
            return False

    def is_usable(self) -> bool:
        with self._lock:
            return not self.stale


def _dyn_rand(entry: Mapping[str, Any]) -> int | None:
    """The entry's `dyn_rand` (argus f3) as an int, or None when absent.

    Coerced here, at the file boundary, because JSON may carry it quoted and
    the value is shifted into a protobuf varint downstream. A non-numeric value
    raises `ValueError`, which `reload` reports the same way it reports a half
    pair — the entry is dropped, never silently un-paired. The message is
    built from the field name only; the value never appears in it."""
    raw = entry.get('dyn_rand')
    if raw is None or raw == '':
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError('dyn_rand is not an integer') from None


def _identity_key(entry: Mapping[str, Any]) -> str:
    dq = entry.get('device_query') or {}
    return str(entry.get('device_id') or dq.get('device_id') or '')


class IdentityStore:
    """Loads warm identities from a JSON file and hot-reloads on mtime change.

    File format — a list of identity objects, or {"identities": [...]}:
        [
          {"device_id": "...", "iid": "...", "cookie": "...",
           "x_tt_token": "...", "user_agent": "...", "device_query": {...},
           "dyn_seed": "...", "dyn_rand": 123}
        ]

    `dyn_seed`/`dyn_rand` are the captured argus `(f24, f3)` pair and are
    OPTIONAL — an entry without them is a normal identity and every path that
    worked before works unchanged. They are a PAIR: an entry carrying one
    without the other is dropped with an ERROR, not loaded half-configured.

    Health (consecutive_empty / stale) is preserved across reloads for identities
    whose key (device_id) is unchanged, so a no-op rewrite doesn't reset counters,
    but an entry with NEW credentials — cookie, x_tt_token or the argus pair —
    is treated as refreshed (health reset)."""

    def __init__(self, path: str | os.PathLike[str], *,
                 stale_after: int = DEFAULT_STALE_AFTER) -> None:
        self._path = os.fspath(path)
        self._stale_after = max(1, stale_after)
        self._lock = threading.Lock()
        self._identities: dict[str, Identity] = {}
        self._mtime: float | None = None
        self.reload(force=True)

    # ---- loading -----------------------------------------------------------
    def _read_file(self) -> list[Mapping[str, Any]]:
        with open(self._path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get('identities') or []
        if not isinstance(data, list):
            raise ValueError('identities file must be a list or {"identities": [...]}')
        return data

    def reload(self, *, force: bool = False) -> bool:
        """Reload if the file changed (or force). Returns True if reloaded.

        Preserves health for unchanged entries; resets health when the
        cookie/x_tt_token changed (i.e. the identity was genuinely refreshed)."""
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            if force:
                logger.warning('identities file %s not found — no warm identities loaded', self._path)
            return False
        if not force and self._mtime is not None and mtime <= self._mtime:
            return False
        try:
            entries = self._read_file()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.error('failed to parse identities file %s: %s', self._path, exc)
            return False

        with self._lock:
            old = self._identities
            new: dict[str, Identity] = {}
            for entry in entries:
                key = _identity_key(entry)
                if not key:
                    logger.warning('skipping identity with no device_id: %r', entry)
                    continue
                cookie = entry.get('cookie')
                token = entry.get('x_tt_token')
                try:
                    ident = Identity(
                        key=key,
                        device_id=key,
                        iid=str(entry.get('iid') or (entry.get('device_query') or {}).get('iid') or ''),
                        cookie=cookie, x_tt_token=token,
                        user_agent=entry.get('user_agent'),
                        device_query=entry.get('device_query') or {},
                        dyn_seed=entry.get('dyn_seed') or None,
                        dyn_rand=_dyn_rand(entry),
                    )
                except ValueError as exc:
                    # A half pair, or a `dyn_rand` that is not a number. The
                    # ENTRY is dropped rather than degraded to no-pair: the
                    # file says this identity carries the pair and it does not,
                    # so serving it as if it never claimed one would hide the
                    # error behind a working search path. Loud, keyed, and
                    # carrying no value from the file.
                    logger.error('skipping identity %s: %s', key, exc)
                    continue
                prev = old.get(key)
                # Carry health forward only if the credentials are unchanged;
                # a new cookie/token/pair means the identity was refreshed → start fresh.
                if (prev is not None and prev.cookie == cookie and prev.x_tt_token == token
                        and prev.dyn_seed == ident.dyn_seed and prev.dyn_rand == ident.dyn_rand):
                    ident.consecutive_empty = prev.consecutive_empty
                    ident.stale = prev.stale
                    ident.last_ok = prev.last_ok
                elif prev is not None:
                    logger.info('identity %s refreshed (new credentials) — health reset', key)
                new[key] = ident
            self._identities = new
            self._mtime = mtime
        # The LOADED count, not the entry count: an entry can be skipped (no
        # device_id, or a half argus pair), and reporting the file's length
        # would say 3 while serving 2 — the skip is already an ERROR line, and
        # this line must not contradict it.
        logger.info('loaded %d warm identit(y/ies) of %d entries from %s (%d usable)',
                    len(self._identities), len(entries), self._path, self.usable_count())
        return True

    # ---- access ------------------------------------------------------------
    def snapshot(self) -> list[Identity]:
        with self._lock:
            return list(self._identities.values())

    def get(self, key: str) -> Identity | None:
        with self._lock:
            return self._identities.get(key)

    def usable_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._identities.values() if i.is_usable())

    @property
    def stale_after(self) -> int:
        return self._stale_after

    def report_ok(self, key: str) -> None:
        ident = self.get(key)
        if ident is not None:
            ident.report_ok()

    def report_empty(self, key: str) -> None:
        ident = self.get(key)
        if ident is not None:
            ident.report_empty(self._stale_after)

    def status(self) -> dict:
        with self._lock:
            return {
                'path': self._path,
                'total': len(self._identities),
                'usable': sum(1 for i in self._identities.values() if i.is_usable()),
                'stale': [i.key for i in self._identities.values() if i.stale],
            }
