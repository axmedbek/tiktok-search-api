from __future__ import annotations
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Generic, Optional, TypeVar
from .client import TikTokClient
from .config import PoolConfig
from .errors import PoolCode, PoolExhausted, SoftError
from .filters import SearchPage, SearchQuery
from .identity_manager import IdentityStore
from .paging import device_handle
logger = logging.getLogger('tiktoksearch.pool')
# A page_token pins the device that owns the TikTok search session. The pin is
# a STABLE handle (hash of the identity key), never the positional `id{i}`
# label: the capture loop can rewrite identities.json with an entry removed,
# added at the front, or reordered, after which `id0` names a DIFFERENT device
# — the pin would succeed and a foreign search_id would go out on a healthy
# warm identity. If the pinned identity is gone the session cannot be continued
# anywhere, so we say so instead of quietly serving another device.
# None of these messages name a device: they are returned to HTTP clients.
PINNED_DEVICE_GONE_MSG = (
    'The device that served the previous page is no longer in the pool — '
    'start a new search without page_token.'
)
PINNED_DEVICE_BUSY_MSG = 'The device that served the previous page is busy — try again shortly.'
PINNED_DEVICE_CAPPED_MSG = 'Daily request cap reached on the device that served the previous page.'
PINNED_DEVICE_STALE_MSG = (
    'The identity that served the previous page went stale (credentials '
    'expired) — start a new search without page_token.'
)
# Guard on an internal invariant — run_merged must never be handed a pinned
# (page_token) query — but raised as a DOMAIN exception, so app.py's central
# PoolExhausted map answers it with a mapped status instead of letting a bare
# ValueError escape the executor as an unhandled 500. Worded for a client,
# because a mapped PoolExhausted reason IS returned to one; names no device.
TOKEN_FAN_OUT_MSG = (
    'A page_token search session lives on a single device and cannot be fanned '
    'out across the pool — send fan_out=1 or drop page_token.'
)


@dataclass(frozen=True, slots=True)
class ServedBy:
    """Who served a search: the human-facing pool label plus the stable,
    non-secret handle a page_token pins on."""
    label: str
    handle: str


class HealthVerdict(Enum):
    """What one pooled call proved about the identity that served it.

    * `OK` — the identity is trusted; clears its consecutive-empty count.
    * `EMPTY` — the reply is evidence of risk-control. It counts toward
      DEFAULT_STALE_AFTER and eventually RETIRES the identity, so it is
      reserved for emptiness that has no innocent explanation.
    * `NEUTRAL` — the call proved nothing either way and is not reported at
      all, neither ok nor empty. This is the verdict for emptiness the
      endpoint's own domain explains: a search-session tail, or a private /
      zero-post account whose posts list is legitimately `[]`. Charging those
      as EMPTY would let three ordinary requests retire the only warm identity
      and 503 every caller.

    A PLAIN Enum, unlike the `(str, Enum)` shape `PoolCode` uses: that mixin
    exists so a code can cross the HTTP boundary as a string, which a verdict
    never does — and it would make the bare string `'ok'` hash and compare
    equal to `OK`, quietly accepting a caller that returned a loose string
    instead of a member.
    """
    OK = 'ok'
    EMPTY = 'empty'
    NEUTRAL = 'neutral'


# `_report`'s `ok=` per verdict; None means "do not report at all". A table
# rather than an if/elif chain so an unrecognised verdict raises KeyError
# instead of falling through to a default — every default here is a bug:
# defaulting to OK would mask a cold identity, EMPTY would retire a healthy
# one, and NEUTRAL would silently disable identity health for that endpoint.
_REPORT_BY_VERDICT: dict[HealthVerdict, Optional[bool]] = {
    HealthVerdict.OK: True,
    HealthVerdict.EMPTY: False,
    HealthVerdict.NEUTRAL: None,
}

_CallResult = TypeVar('_CallResult')


@dataclass(frozen=True, slots=True)
class CallOutcome(Generic[_CallResult]):
    """What a `run_call` callable hands back: its result AND its verdict.

    Both fields are REQUIRED, deliberately. The verdict cannot be inferred
    from the result — no records is risk-control evidence on a first search
    page and an ordinary answer for a private account's posts — so any default
    would be wrong for half the callers. A call that forgets it therefore gets
    a TypeError from this constructor, at the line with the bug, rather than
    being silently treated as ok or as empty."""
    result: _CallResult
    verdict: HealthVerdict


def _search_verdict(page: SearchPage, *, continuation: bool) -> HealthVerdict:
    """How a completed search reflects on the identity that served it.

    An EMPTY PAGE is judged as identity evidence on first pages only: a
    continuation carries a search_id, and an empty answer to one is the tail
    of that session, not risk-control. Counting those would let three ordinary
    "load more" tails (DEFAULT_STALE_AFTER) retire the only warm identity and
    503 every caller.

    A SoftError is reported either way — by `run_call`, which owns every
    exception path, not here. Session-shaped emptiness never reaches this
    point any more — `client._get_signed` returns it as data — so a SoftError
    that still escapes on a continuation is genuine risk-control evidence (a
    non-zero `status_code`, or a nil with has_more=true), and anti-block
    invariant (b) needs IdentityStore to see it. It is not abusable: page
    tokens are HMAC-bound, so a replay that trips report_empty three times
    means the identity really is answering risk-control-shaped.

    This rule is SEARCH's, which is why it lives beside `run` rather than
    inside `run_call`: it must NOT be applied to the posts endpoint, where a
    private or zero-post account genuinely returns nothing and three such
    lookups would retire an identity that did nothing wrong. That endpoint's
    rule is `posts_verdict` immediately below — the two are deliberately
    neighbours, so a change to one is read against the other.
    """
    if page.records:
        return HealthVerdict.OK
    if continuation:
        return HealthVerdict.NEUTRAL
    return HealthVerdict.EMPTY


def posts_verdict(page: SearchPage) -> HealthVerdict:
    """How a completed `/user/posts` page reflects on the identity.

    PUBLIC and living here, beside `_search_verdict`, because the two are one
    policy in two halves: identity-health rules are what anti-block review
    reads this module for, and a second rule hidden in the HTTP layer is a rule
    nobody checks against its sibling. `run_call` still takes the verdict from
    the caller's `fn` — this is the rule, not its application.

    Deliberately NOT `_search_verdict`, which is search's own: that one charges
    an empty FIRST page as EMPTY evidence, and three of those retire the only
    warm identity and 503 every caller. On this endpoint an empty page has
    innocent explanations that say nothing about risk-control — a session that
    has run dry for a handle search surfaces little of, and (once the real
    posts endpoint serves this) a private or zero-post account — so it is
    NEUTRAL: reported neither ok nor empty. That is also why it takes no
    `continuation` flag: first page and tail are treated alike here, so there
    is nothing for the caller to get wrong.

    Genuine risk-control still reaches identity health, which is what makes
    NEUTRAL safe: `client._get_signed` raises SoftError for a search reply that
    carried no payload — including every sessionless empty first page — and
    `run_call` reports EVERY escaping SoftError as an empty whatever the
    verdict would have said.

    Judged on the UNFILTERED page: the author filter is this service's own
    doing, and dropping every record of a page the identity really did serve
    says nothing about the identity."""
    if page.records:
        return HealthVerdict.OK
    return HealthVerdict.NEUTRAL


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')

def _mask_proxy(proxy: Optional[str]) -> Optional[str]:
    if not proxy:
        return None
    rest = proxy.split('://', 1)[-1]
    return rest.split('@', 1)[1] if '@' in rest else rest

class DeviceSlot:

    def __init__(self, client: TikTokClient, daily_cap: int, label: str,
                 identity_key: Optional[str] = None) -> None:
        self.client = client
        self.label = label
        self.identity_key = identity_key
        self.daily_cap = daily_cap
        self._used = 0
        self._day = _utc_day()
        self._last_used = 0.0
        self.inflight = threading.Lock()
        self._lock = threading.Lock()

    def _roll_day(self) -> None:
        today = _utc_day()
        if today != self._day:
            self._day, self._used = (today, 0)

    def remaining(self) -> int:
        with self._lock:
            self._roll_day()
            return max(0, self.daily_cap - self._used)

    def try_reserve(self, now: float) -> bool:
        with self._lock:
            self._roll_day()
            if self._used >= self.daily_cap:
                return False
            self._used += 1
            self._last_used = now
            return True

    @property
    def last_used(self) -> float:
        return self._last_used

    @property
    def handle(self) -> str:
        """Stable pin handle for this slot. Derived by hash from the identity
        key when there is one, else from the client's device_id (static and
        synthetic slots have no identity) — the raw value never leaves the
        process, and unlike the positional label it cannot come to name a
        different device after identities.json is reordered."""
        return device_handle(self.identity_key or self.client.device_id)

    @property
    def served_by(self) -> ServedBy:
        return ServedBy(label=self.label, handle=self.handle)

    def status(self) -> dict:
        with self._lock:
            self._roll_day()
            return {'label': self.label, 'device_id': self.client.device_id, 'iid': self.client.iid, 'proxy': _mask_proxy(self.client.proxy), 'used_today': self._used, 'daily_cap': self.daily_cap, 'remaining_today': max(0, self.daily_cap - self._used), 'busy': self.inflight.locked()}

class ClientPool:

    def __init__(self, config: PoolConfig, *, identities: Optional[IdentityStore] = None) -> None:
        self._config = config
        self._identities = identities
        self._slots = self._build_slots(config, identities)
        self._cond = threading.Condition()
        proxied = sum((1 for s in self._slots if s.client.proxy))
        logger.info('Client pool: %d device(s), %d req/device/day (total %d/day), %d proxied / %d direct.', len(self._slots), config.daily_request_cap_per_device, self.total_daily_capacity(), proxied, len(self._slots) - proxied)

    @staticmethod
    def _build_slots(config: PoolConfig, identities: Optional[IdentityStore] = None) -> list[DeviceSlot]:
        proxies = list(config.proxies)
        proxy_idx = 0

        def next_proxy() -> Optional[str]:
            nonlocal proxy_idx
            if not proxies:
                return None
            proxy = proxies[proxy_idx % len(proxies)]
            proxy_idx += 1
            return proxy
        slots: list[DeviceSlot] = []
        cap = config.daily_request_cap_per_device
        # Hot-reloadable warm identities take precedence over the static
        # config.devices list — they carry live cookie/x-tt-token refreshed by
        # the capture loop. See identity_manager.
        if identities is not None:
            for i, ident in enumerate(identities.snapshot()):
                overrides = ident.overrides()
                overrides['proxy'] = next_proxy()
                client_cfg = config.client_defaults.with_overrides(overrides)
                slots.append(DeviceSlot(TikTokClient(client_cfg), cap, f'id{i}', identity_key=ident.key))
            if slots:
                return slots
            logger.warning('IdentityStore has no usable identities — falling back to config devices.')
        for i, device_cfg in enumerate(config.devices):
            overrides = dict(device_cfg)
            overrides.setdefault('proxy', None)
            if not overrides.get('proxy'):
                overrides['proxy'] = next_proxy()
            client_cfg = config.client_defaults.with_overrides(overrides)
            slots.append(DeviceSlot(TikTokClient(client_cfg), cap, f'dev{i}'))
        for j in range(config.synthetic_devices):
            client_cfg = config.client_defaults.with_overrides({'proxy': next_proxy()})
            slots.append(DeviceSlot(TikTokClient(client_cfg), cap, f'syn{j}'))
        if not slots:
            client_cfg = config.client_defaults.with_overrides({'proxy': next_proxy()})
            slots.append(DeviceSlot(TikTokClient(client_cfg), cap, 'syn0'))
            logger.warning('No devices configured — running with 1 synthetic device.')
        return slots

    def _rebuild_from_identities(self) -> None:
        """Rebuild slots after the identity file hot-reloaded. Called under no
        lock; acquires the condition to swap the slot list atomically."""
        if self._identities is None:
            return
        new_slots = self._build_slots(self._config, self._identities)
        with self._cond:
            self._slots = new_slots
            self._cond.notify_all()

    def total_daily_capacity(self) -> int:
        return sum((s.daily_cap for s in self._slots))

    def _usable(self, slot: DeviceSlot) -> bool:
        """A slot is usable if it has budget AND its identity (if any) is not stale."""
        if self._identities is None or slot.identity_key is None:
            return True
        ident = self._identities.get(slot.identity_key)
        return ident is None or ident.is_usable()

    def acquire(self, handle: Optional[str]=None) -> DeviceSlot:
        """Reserve an idle slot. With `handle`, only the slot whose STABLE
        handle matches is acceptable: a TikTok search session (page_token) is
        bound to the device that opened it, so falling back to another device
        would silently invalidate it — and matching on the positional label
        instead could hand the session to a different identity entirely."""
        deadline = time.monotonic() + self._config.acquire_timeout_s
        # Pick up a freshly-captured identities.json before we look for a slot.
        if self._identities is not None and self._identities.reload():
            self._rebuild_from_identities()
        with self._cond:
            while True:
                candidates = self._slots
                if handle is not None:
                    candidates = [s for s in self._slots if s.handle == handle]
                    if not candidates:
                        raise PoolExhausted(PINNED_DEVICE_GONE_MSG, code=PoolCode.GONE)
                budgeted = [s for s in candidates if self._usable(s) and s.remaining() > 0]
                if not budgeted:
                    if handle is not None:
                        pinned = candidates[0]
                        if self._usable(pinned):
                            raise PoolExhausted(PINNED_DEVICE_CAPPED_MSG, code=PoolCode.CAP)
                        raise PoolExhausted(PINNED_DEVICE_STALE_MSG, code=PoolCode.STALE)
                    if self._identities is not None and self._identities.usable_count() == 0:
                        raise PoolExhausted(
                            'All warm identities are stale (cookie/x-tt-token expired) — '
                            'refresh identities.json via the capture loop.',
                            code=PoolCode.STALE)
                    raise PoolExhausted('Daily request cap reached on all devices.', code=PoolCode.CAP)
                idle = sorted((s for s in budgeted if not s.inflight.locked()), key=lambda s: s.last_used)
                if idle:
                    slot = idle[0]
                    slot.inflight.acquire()
                    if not slot.try_reserve(time.monotonic()):
                        slot.inflight.release()
                        continue
                    return slot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if handle is not None:
                        raise PoolExhausted(PINNED_DEVICE_BUSY_MSG, code=PoolCode.BUSY)
                    raise PoolExhausted('All devices busy — try again shortly.', code=PoolCode.BUSY)
                self._cond.wait(timeout=min(remaining, 1.0))

    def release(self, slot: DeviceSlot) -> None:
        with self._cond:
            if slot.inflight.locked():
                slot.inflight.release()
            self._cond.notify_all()

    def run_call(self, fn: Callable[[TikTokClient], CallOutcome[_CallResult]],
                 *, handle: Optional[str]=None) -> tuple[ServedBy, _CallResult]:
        """Serve one call on one pooled device. `handle` pins the device.

        Everything the pool does AROUND a call lives here and nowhere else:
        `acquire` (including the page_token device pin), the daily-cap
        reservation it performs, the identity-health `_report` and `release`.
        `run` is a thin wrapper over this, so a second endpoint cannot ship a
        second, subtly different copy of that machinery.

        `fn` returns its result together with an explicit `HealthVerdict`,
        because only `fn` knows what its own emptiness means — see
        `HealthVerdict` and `_search_verdict`.

        A `SoftError` is reported as empty whatever `fn` would have said, and
        it is the ONLY exception class that reports anything. `client.py`
        picks its exception classes knowing that, and two of those choices
        rest on it: `TransportError` is what a well-formed-but-odd payload
        raises precisely so a healthy identity is not charged for it, and
        `NotFound` is deliberately outside the `SoftError` hierarchy so a
        username the caller made up costs the identity nothing. Broadening
        this `except` would silently undo both arguments. `ValueError` is not
        caught either: the ValueErrors on this path are `paging.encode` /
        `decode` failures raised in `api/app.py` after this returns, and
        swallowing that class here would hide a real producer bug.
        """
        slot = self.acquire(handle)
        try:
            outcome = fn(slot.client)
        except SoftError:
            # Every attempt came back empty/shadow-blocked → penalize the identity.
            self._report(slot, ok=False)
            raise
        else:
            ok = _REPORT_BY_VERDICT[outcome.verdict]
            if ok is not None:
                self._report(slot, ok=ok)
            return (slot.served_by, outcome.result)
        finally:
            self.release(slot)

    def run(self, query: SearchQuery, *, handle: Optional[str]=None) -> tuple[ServedBy, SearchPage]:
        """Serve one search. `handle` pins the device (page_token continuation).

        A thin wrapper over `run_call`: the only search-specific part left is
        `_search_verdict`, which carries the empty-page reasoning."""
        continuation = query.page_token is not None

        def call(client: TikTokClient) -> CallOutcome[SearchPage]:
            page = client.search(query)
            return CallOutcome(result=page,
                               verdict=_search_verdict(page, continuation=continuation))

        return self.run_call(call, handle=handle)

    def _report(self, slot: DeviceSlot, *, ok: bool) -> None:
        if self._identities is None or slot.identity_key is None:
            return
        if ok:
            self._identities.report_ok(slot.identity_key)
        else:
            self._identities.report_empty(slot.identity_key)

    def run_merged(self, query: SearchQuery, fan_out: int) -> tuple[list[str], SearchPage]:
        if query.page_token is not None:
            # A token pins ONE device's search session, and this method's
            # fan_out==1 shortcut calls run() with no handle — it would drop
            # the pin silently and send a foreign search_id. app.py coerces
            # fan_out to 1 for a token (and 422s an explicit fan_out > 1), so
            # this is unreachable today; the invariant belongs where it can be
            # violated, not only where it currently is not.
            #
            # GONE is the closest existing code and the honest one: like a
            # vanished pinned device, this session cannot be continued here, and
            # the answer is to start over — no new exception class for a
            # defence-in-depth branch.
            raise PoolExhausted(TOKEN_FAN_OUT_MSG, code=PoolCode.GONE)
        fan_out = max(1, min(fan_out, len(self._slots)))
        if fan_out == 1:
            served, page = self.run(query)
            return ([served.label], page)

        def one(_: int) -> tuple[ServedBy, SearchPage] | None:
            try:
                return self.run(query)
            except PoolExhausted:
                return None

        with ThreadPoolExecutor(max_workers=fan_out) as pool:
            outcomes = list(pool.map(one, range(fan_out)))

        pages = [o for o in outcomes if o is not None]
        if not pages:
            raise PoolExhausted('Daily request cap reached on all devices.', code=PoolCode.CAP)

        merged: list[dict] = []
        seen: set[str] = set()
        has_more = False
        for _, page in pages:
            has_more = has_more or page.has_more
            for record in page.records:
                key = record.get('id') or record.get('username')
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(record)
        devices = [served.label for served, _ in pages]
        merged = merged[:query.limit]
        return (devices, SearchPage(records=merged, cursor=query.cursor, next_cursor=query.cursor + len(merged) if has_more else None, has_more=has_more))

    def status(self) -> dict:
        slots = [s.status() for s in self._slots]
        out = {'devices': slots, 'device_count': len(slots), 'idle': sum((1 for s in slots if not s['busy'])), 'total_daily_capacity': self.total_daily_capacity(), 'capacity_remaining_today': sum((s['remaining_today'] for s in slots))}
        if self._identities is not None:
            out['identities'] = self._identities.status()
        return out
