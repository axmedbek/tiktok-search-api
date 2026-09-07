from __future__ import annotations
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
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

    def run(self, query: SearchQuery, *, handle: Optional[str]=None) -> tuple[ServedBy, SearchPage]:
        """Serve one search. `handle` pins the device (page_token continuation).

        An EMPTY PAGE is judged as identity evidence on first pages only: a
        continuation carries a search_id, and an empty answer to one is the tail
        of that session, not risk-control. Counting those would let three
        ordinary "load more" tails (DEFAULT_STALE_AFTER) retire the only warm
        identity and 503 every caller.

        A SoftError is reported either way. Session-shaped emptiness never
        reaches here any more — `client._get_signed` returns it as data — so a
        SoftError that still escapes on a continuation is genuine risk-control
        evidence (a non-zero `status_code`, or a nil with has_more=true), and
        anti-block invariant (b) needs IdentityStore to see it. It is not
        abusable: page tokens are HMAC-bound, so a replay that trips
        report_empty three times means the identity really is answering
        risk-control-shaped."""
        slot = self.acquire(handle)
        continuation = query.page_token is not None
        try:
            page = slot.client.search(query)
        except SoftError:
            # Every attempt came back empty/shadow-blocked → penalize the identity.
            self._report(slot, ok=False)
            raise
        else:
            if page.records:
                self._report(slot, ok=True)
            elif not continuation:
                self._report(slot, ok=False)
            return (slot.served_by, page)
        finally:
            self.release(slot)

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
