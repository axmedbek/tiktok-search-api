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
    # health
    consecutive_empty: int = 0
    stale: bool = False
    last_ok: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

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
        return out

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


def _identity_key(entry: Mapping[str, Any]) -> str:
    dq = entry.get('device_query') or {}
    return str(entry.get('device_id') or dq.get('device_id') or '')


class IdentityStore:
    """Loads warm identities from a JSON file and hot-reloads on mtime change.

    File format — a list of identity objects, or {"identities": [...]}:
        [
          {"device_id": "...", "iid": "...", "cookie": "...",
           "x_tt_token": "...", "user_agent": "...", "device_query": {...}}
        ]

    Health (consecutive_empty / stale) is preserved across reloads for identities
    whose key (device_id) is unchanged, so a no-op rewrite doesn't reset counters,
    but an entry with a NEW cookie/token is treated as refreshed (health reset)."""

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
                ident = Identity(
                    key=key,
                    device_id=key,
                    iid=str(entry.get('iid') or (entry.get('device_query') or {}).get('iid') or ''),
                    cookie=cookie, x_tt_token=token,
                    user_agent=entry.get('user_agent'),
                    device_query=entry.get('device_query') or {},
                )
                prev = old.get(key)
                # Carry health forward only if the credentials are unchanged;
                # a new cookie/token means the identity was refreshed → start fresh.
                if prev is not None and prev.cookie == cookie and prev.x_tt_token == token:
                    ident.consecutive_empty = prev.consecutive_empty
                    ident.stale = prev.stale
                    ident.last_ok = prev.last_ok
                elif prev is not None:
                    logger.info('identity %s refreshed (new cookie/token) — health reset', key)
                new[key] = ident
            self._identities = new
            self._mtime = mtime
        logger.info('loaded %d warm identit(y/ies) from %s (%d usable)',
                    len(entries), self._path, self.usable_count())
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
