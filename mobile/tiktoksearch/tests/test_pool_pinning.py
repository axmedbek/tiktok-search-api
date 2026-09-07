"""Unit tests for pool.py: pinning a search session to a device, and what
identity health is allowed to learn from an empty page.

A page_token pins the device that owns the TikTok search session. The pin is a
hashed, stable handle, NOT the positional `id{i}` label — the capture loop
rewrites identities.json with entries added, removed or reordered, after which
`id0` names a different device, the pin would still "succeed", and a foreign
search_id would go out on a healthy warm identity. These tests drive that
rewrite for real (temp file + `os.utime`, synthetic ids, fake cookie);
`mobile/identities.json` is never read.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    FAKE_COOKIE,
    STUB_SIGNER_KEY,
    identity,
    write_identities,
)

from tiktoksearch import pool as pool_module  # noqa: E402
from tiktoksearch.config import PoolConfig  # noqa: E402
from tiktoksearch.errors import PoolCode, PoolExhausted, SoftError  # noqa: E402
from tiktoksearch.filters import SearchKind, SearchPage, SearchQuery  # noqa: E402
from tiktoksearch.identity_manager import DEFAULT_STALE_AFTER, IdentityStore  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    device_handle,
)
from tiktoksearch.pool import ClientPool  # noqa: E402
from tiktoksearch.client import SEARCH_VIDEO_PATH  # noqa: E402

DEVICE_A = 'DEVA'
DEVICE_B = 'DEVB'
REFRESHED_COOKIE = 'sessionid=FAKE-COOKIE-2'
_MODULE_TREE = ast.parse(inspect.getsource(pool_module))


class Fixture:
    """A pool over a synthetic, rewritable identities file."""

    def __init__(self, tmp_path: Path, entries: list[dict], **config_over) -> None:
        self.path = tmp_path / 'ids.json'
        self._stamp = 1_700_000_000
        self.write(entries)
        mapping = {'rapidapi_key': STUB_SIGNER_KEY, 'acquire_timeout_s': 0.05}
        mapping.update(config_over)
        self.store = IdentityStore(self.path)
        self.pool = ClientPool(PoolConfig.from_mapping(mapping), identities=self.store)

    def write(self, entries: list[dict]) -> None:
        self._stamp += 10
        write_identities(self.path, entries, stamp=self._stamp)

    def labels(self) -> dict[str, str]:
        """label -> device_id, as the pool currently sees it."""
        return {slot.label: slot.client.device_id for slot in self.pool._slots}

    def slot_for(self, device_id: str):
        return next(s for s in self.pool._slots if s.client.device_id == device_id)


class FakeClient:
    """Stands in for TikTokClient inside a slot: no signing, no transport."""

    proxy = None

    def __init__(self, device_id: str, result) -> None:
        self.device_id = device_id
        self.iid = f'IID-{device_id}'
        self._result = result
        self.searches = 0

    def search(self, query: SearchQuery) -> SearchPage:
        self.searches += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _page(records: list[dict]) -> SearchPage:
    return SearchPage(records=records, cursor=0, next_cursor=None, has_more=False)


def _token(device_id: str = DEVICE_A) -> PageToken:
    return PageToken(version=TOKEN_VERSION, query_hash='q' * 32,
                     device_handle=device_handle(device_id),
                     endpoints=(EndpointState(SEARCH_VIDEO_PATH, 30, 'SIDP', True, True),))


def _query(*, token: PageToken | None = None) -> SearchQuery:
    return SearchQuery(kind=SearchKind.KEYWORD, term='ocean', limit=20, page_token=token)


class TestPinResolvesByStableHandle:
    def test_the_handle_is_derived_from_the_identity_not_the_label(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        slot = fixture.slot_for(DEVICE_A)
        assert slot.handle == device_handle(DEVICE_A)
        assert slot.handle != slot.label
        assert slot.served_by.label == 'id0'
        assert slot.served_by.handle == slot.handle

    def test_distinct_devices_get_distinct_handles(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        handles = {slot.handle for slot in fixture.pool._slots}
        assert len(handles) == 2

    def test_the_pin_follows_the_device_across_an_identities_reorder(self, tmp_path):
        # THE reason the pin is not the label: after this rewrite `id0` is a
        # DIFFERENT device, and a positional pin would send the session's
        # search_id out on it.
        fixture = Fixture(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        assert fixture.labels() == {'id0': DEVICE_A, 'id1': DEVICE_B}

        fixture.write([identity(DEVICE_B), identity(DEVICE_A)])
        slot = fixture.pool.acquire(handle=device_handle(DEVICE_A))
        try:
            assert slot.client.device_id == DEVICE_A
            assert slot.label == 'id1'            # the label moved, the pin did not
            assert fixture.labels()['id0'] == DEVICE_B
        finally:
            fixture.pool.release(slot)

    def test_the_pin_survives_a_credential_refresh_of_the_same_device(self, tmp_path):
        # The capture loop rewrites the same device with a fresh cookie; the
        # session belongs to the device, so the pin must still resolve.
        fixture = Fixture(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        assert fixture.store.get(DEVICE_A).cookie == FAKE_COOKIE

        fixture.write([identity(DEVICE_B),
                       identity(DEVICE_A, cookie=REFRESHED_COOKIE)])
        slot = fixture.pool.acquire(handle=device_handle(DEVICE_A))
        try:
            assert slot.client.device_id == DEVICE_A
            assert slot.handle == device_handle(DEVICE_A)
            assert fixture.store.get(DEVICE_A).cookie == REFRESHED_COOKIE
        finally:
            fixture.pool.release(slot)

    def test_a_refreshed_credential_makes_a_retired_pin_usable_again(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        for _ in range(DEFAULT_STALE_AFTER):
            fixture.store.report_empty(DEVICE_A)
        assert fixture.store.usable_count() == 0

        fixture.write([identity(DEVICE_A, cookie=REFRESHED_COOKIE)])
        slot = fixture.pool.acquire(handle=device_handle(DEVICE_A))
        try:
            assert slot.client.device_id == DEVICE_A
        finally:
            fixture.pool.release(slot)


class TestPinnedFailures:
    def test_a_vanished_pin_is_gone_not_served_by_another_device(self, tmp_path, transport):
        fixture = Fixture(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        fixture.write([identity(DEVICE_B)])
        with pytest.raises(PoolExhausted) as excinfo:
            fixture.pool.acquire(handle=device_handle(DEVICE_A))
        assert excinfo.value.code is PoolCode.GONE
        # Refused BEFORE anything is signed: a vanished pin costs no money.
        assert transport.calls == []

    def test_the_gone_message_names_no_device_and_no_credential(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        fixture.write([identity(DEVICE_B)])
        with pytest.raises(PoolExhausted) as excinfo:
            fixture.pool.acquire(handle=device_handle(DEVICE_A))
        reason = excinfo.value.reason
        for secret in (DEVICE_A, DEVICE_B, FAKE_COOKIE, 'sessionid'):
            assert secret not in reason

    def test_a_pin_over_its_daily_cap_is_cap(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)],
                          daily_request_cap_per_device=1)
        handle = device_handle(DEVICE_A)
        fixture.pool.release(fixture.pool.acquire(handle=handle))
        with pytest.raises(PoolExhausted) as excinfo:
            fixture.pool.acquire(handle=handle)
        assert excinfo.value.code is PoolCode.CAP

    def test_a_pin_whose_identity_went_stale_is_stale(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        for _ in range(DEFAULT_STALE_AFTER):
            fixture.store.report_empty(DEVICE_A)
        with pytest.raises(PoolExhausted) as excinfo:
            fixture.pool.acquire(handle=device_handle(DEVICE_A))
        assert excinfo.value.code is PoolCode.STALE

    def test_a_busy_pin_is_busy(self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        handle = device_handle(DEVICE_A)
        held = fixture.pool.acquire(handle=handle)
        try:
            with pytest.raises(PoolExhausted) as excinfo:
                fixture.pool.acquire(handle=handle)
            assert excinfo.value.code is PoolCode.BUSY
        finally:
            fixture.pool.release(held)


class TestPoolExhaustedCodes:
    def test_every_raise_site_in_pool_py_passes_an_explicit_code(self):
        # app.py maps the STATUS off this enum, so an omitted code is a silently
        # wrong HTTP status rather than a visible error.
        sites = [node for node in ast.walk(_MODULE_TREE)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name)
                 and node.func.id == 'PoolExhausted']
        assert sites, 'no PoolExhausted raise site found — did pool.py move?'
        for site in sites:
            keywords = {kw.arg for kw in site.keywords}
            assert 'code' in keywords, ast.unparse(site)

    def test_the_default_code_fails_safe_to_busy(self):
        # BUSY maps to 503 (retryable), not 429 — an omission must not tell a
        # caller they are rate-limited.
        assert PoolExhausted('reason').code is PoolCode.BUSY


class TestIdentityHealthAccounting:
    """Invariant (b), split precisely. A session TAIL is not risk-control
    evidence; a SoftError escaping a continuation is."""

    def _run(self, tmp_path, result, *, continuation: bool):
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        slot = fixture.slot_for(DEVICE_A)
        slot.client = FakeClient(DEVICE_A, result)
        query = _query(token=_token() if continuation else None)
        return fixture, query

    def test_a_continuation_tail_is_not_charged_to_identity_health(self, tmp_path):
        # Three ordinary "load more" tails would otherwise retire the only warm
        # identity (DEFAULT_STALE_AFTER) and 503 every caller.
        fixture, query = self._run(tmp_path, _page([]), continuation=True)
        served, page = fixture.pool.run(query, handle=device_handle(DEVICE_A))
        assert page.records == []
        assert served.handle == device_handle(DEVICE_A)
        assert fixture.store.get(DEVICE_A).consecutive_empty == 0

    def test_a_continuation_soft_error_is_charged_to_identity_health(self, tmp_path):
        # Session-shaped emptiness never reaches pool.run any more, so a
        # SoftError here is genuine risk-control and must be seen.
        fixture, query = self._run(tmp_path, SoftError('empty'), continuation=True)
        with pytest.raises(SoftError):
            fixture.pool.run(query, handle=device_handle(DEVICE_A))
        assert fixture.store.get(DEVICE_A).consecutive_empty == 1

    def test_a_sessionless_empty_page_is_charged_to_identity_health(self, tmp_path):
        fixture, query = self._run(tmp_path, _page([]), continuation=False)
        fixture.pool.run(query)
        assert fixture.store.get(DEVICE_A).consecutive_empty == 1

    def test_records_clear_the_empty_counter(self, tmp_path):
        fixture, query = self._run(tmp_path, _page([{'id': '1'}]), continuation=True)
        fixture.store.report_empty(DEVICE_A)
        fixture.pool.run(query, handle=device_handle(DEVICE_A))
        assert fixture.store.get(DEVICE_A).consecutive_empty == 0


class TestRunMerged:
    def test_run_merged_refuses_a_token_query_as_a_domain_error(self, tmp_path):
        # Defence in depth: its fan_out==1 shortcut calls run() with no handle,
        # which would drop the pin silently. It must be a MAPPED PoolExhausted,
        # not a bare ValueError escaping the executor as a 500.
        fixture = Fixture(tmp_path, [identity(DEVICE_A)])
        with pytest.raises(PoolExhausted) as excinfo:
            fixture.pool.run_merged(_query(token=_token()), 1)
        assert excinfo.value.code is PoolCode.GONE
        assert not isinstance(excinfo.value, ValueError)

    def test_a_merged_page_carries_no_resumable_state_to_mint_a_token_from(
            self, tmp_path):
        fixture = Fixture(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        for device in (DEVICE_A, DEVICE_B):
            fixture.slot_for(device).client = FakeClient(
                device, _page([{'id': device}]))
        devices, page = fixture.pool.run_merged(_query(), 2)
        assert sorted(devices) == ['id0', 'id1']
        assert page.endpoints == ()
        assert page.seen == ()
