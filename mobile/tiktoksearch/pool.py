from __future__ import annotations
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional
from .client import TikTokClient
from .config import PoolConfig
from .errors import PoolExhausted, SoftError
from .filters import SearchPage, SearchQuery
from .identity_manager import IdentityStore
logger = logging.getLogger('tiktoksearch.pool')

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

    def acquire(self) -> DeviceSlot:
        deadline = time.monotonic() + self._config.acquire_timeout_s
        # Pick up a freshly-captured identities.json before we look for a slot.
        if self._identities is not None and self._identities.reload():
            self._rebuild_from_identities()
        with self._cond:
            while True:
                budgeted = [s for s in self._slots if self._usable(s) and s.remaining() > 0]
                if not budgeted:
                    if self._identities is not None and self._identities.usable_count() == 0:
                        raise PoolExhausted(
                            'All warm identities are stale (cookie/x-tt-token expired) — '
                            'refresh identities.json via the capture loop.')
                    raise PoolExhausted('Daily request cap reached on all devices.')
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
                    raise PoolExhausted('All devices busy — try again shortly.')
                self._cond.wait(timeout=min(remaining, 1.0))

    def release(self, slot: DeviceSlot) -> None:
        with self._cond:
            if slot.inflight.locked():
                slot.inflight.release()
            self._cond.notify_all()

    def run(self, query: SearchQuery) -> tuple[str, SearchPage]:
        slot = self.acquire()
        try:
            page = slot.client.search(query)
        except SoftError:
            # Every attempt came back empty/shadow-blocked → penalize the identity.
            self._report(slot, ok=False)
            raise
        else:
            self._report(slot, ok=bool(page.records))
            return (slot.label, page)
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
        fan_out = max(1, min(fan_out, len(self._slots)))
        if fan_out == 1:
            device, page = self.run(query)
            return ([device], page)

        def one(_: int) -> tuple[str, SearchPage] | None:
            try:
                return self.run(query)
            except PoolExhausted:
                return None

        with ThreadPoolExecutor(max_workers=fan_out) as pool:
            outcomes = list(pool.map(one, range(fan_out)))

        pages = [o for o in outcomes if o is not None]
        if not pages:
            raise PoolExhausted('Daily request cap reached on all devices.')

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
        devices = [device for device, _ in pages]
        merged = merged[:query.limit]
        return (devices, SearchPage(records=merged, cursor=query.cursor, next_cursor=query.cursor + len(merged) if has_more else None, has_more=has_more))

    def status(self) -> dict:
        slots = [s.status() for s in self._slots]
        out = {'devices': slots, 'device_count': len(slots), 'idle': sum((1 for s in slots if not s['busy'])), 'total_daily_capacity': self.total_daily_capacity(), 'capacity_remaining_today': sum((s['remaining_today'] for s in slots))}
        if self._identities is not None:
            out['identities'] = self._identities.status()
        return out
