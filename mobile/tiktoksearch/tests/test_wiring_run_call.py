"""Stubbed wiring/contract checks for the `pool.run_call` seam (Epic subtask 5).

These are COMPOSITION checks over the real `acquire` → `fn` → `_report` →
`release` chain, not unit tests of a mapper. Nothing leaves the process: the
identity store is a synthetic temp file (`mobile/identities.json` is never
read), the slot's client is either a local fake or the real `signer: local`
client over `FakeTransport`, and conftest's session-wide `HTTPAdapter.send`
tripwire still holds.

Four properties, and the first is anti-block invariant (b):

1. `NEUTRAL` reports NOTHING — neither `report_ok` nor `report_empty`. Asserted
   from a NON-ZERO consecutive-empty count, because that is the only starting
   point where a reset and a no-op are distinguishable. `NEUTRAL` mapped to
   `True` would make every session tail / private-account lookup call
   `report_ok`, which CLEARS the counter: an identity under genuine
   risk-control could then never reach `DEFAULT_STALE_AFTER`, because tails
   reset the count faster than empties accumulate, and every caller keeps
   being served by a dying identity.
2. An UNRECOGNISED verdict raises instead of defaulting. That is the stated
   reason `_REPORT_BY_VERDICT` is a table and not an `if/elif/else`: every
   default is a bug (OK masks a cold identity, EMPTY retires a healthy one,
   NEUTRAL silently disables identity health for that endpoint). It must raise,
   report nothing, and still release the slot. `HealthVerdict` being a PLAIN
   Enum is load-bearing for this: with `PoolCode`'s `(str, Enum)` mixin the
   bare string `'ok'` would compare AND hash equal to a member, so the table
   lookup would quietly succeed on a caller that returned a loose string.
3. `CallOutcome` requires BOTH fields. The verdict cannot be inferred from the
   result — no records is risk-control evidence on a first search page and an
   ordinary answer for a private account's posts — so a default would be wrong
   for half the callers.
4. `run_call` honours `handle=` (the page_token device pin, including its
   gone / capped / stale outcomes) and consumes EXACTLY one daily-cap unit —
   per `run_call`, NOT per signed request, which is the cost model a second
   endpoint has to be built against.
5. `SoftError` is the ONLY exception class that reports anything. `client.py`
   picks its exception classes knowing that: `TransportError` is what an odd
   payload raises precisely so a healthy identity is not charged, and
   `NotFound` sits outside the `SoftError` hierarchy so a made-up username
   costs the identity nothing. Broadening that `except` undoes both arguments.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    STUB_SIGNER_KEY,
    identity,
    reply,
    write_identities,
)

from tiktoksearch.config import PoolConfig  # noqa: E402
from tiktoksearch.errors import (  # noqa: E402
    NotFound,
    PoolCode,
    PoolExhausted,
    SoftError,
    TransportError,
)
from tiktoksearch.filters import SearchKind, SearchPage, SearchQuery  # noqa: E402
from tiktoksearch.identity_manager import DEFAULT_STALE_AFTER, IdentityStore  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    device_handle,
)
from tiktoksearch.client import SEARCH_VIDEO_PATH  # noqa: E402
from tiktoksearch.pool import (  # noqa: E402
    _REPORT_BY_VERDICT,
    CallOutcome,
    ClientPool,
    HealthVerdict,
)

DEVICE_A = 'DEVA'
DEVICE_B = 'DEVB'
# One below DEFAULT_STALE_AFTER: a counter parked here is visible in BOTH
# directions — one report_empty retires the identity, one report_ok clears it.
ALMOST_STALE = DEFAULT_STALE_AFTER - 1


def _pool(tmp_path, entries=None, **config_over) -> tuple[ClientPool, IdentityStore]:
    """A pool over a synthetic, rewritable identities file."""
    path = tmp_path / 'ids.json'
    write_identities(path, entries or [identity(DEVICE_A)], stamp=1_700_000_000)
    mapping = {'rapidapi_key': STUB_SIGNER_KEY, 'acquire_timeout_s': 0.05,
               'signer': 'local'}
    mapping.update(config_over)
    store = IdentityStore(path)
    return ClientPool(PoolConfig.from_mapping(mapping), identities=store), store


def _slot(pool: ClientPool, device_id: str = DEVICE_A):
    return next(s for s in pool._slots if s.client.device_id == device_id)


def _reports(pool: ClientPool, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Ledger of `_report` calls: one `ok=` per report actually emitted.

    The counter alone cannot tell "reported ok" from "not reported" once it is
    already zero, so the two are also separated here at the seam itself."""
    original = pool._report
    seen: list[bool] = []

    def spy(slot, *, ok: bool) -> None:
        seen.append(ok)
        original(slot, ok=ok)

    monkeypatch.setattr(pool, '_report', spy)
    return seen


def _page(records: list[dict]) -> SearchPage:
    return SearchPage(records=records, cursor=0, next_cursor=None, has_more=False)


def _token(device_id: str = DEVICE_A) -> PageToken:
    return PageToken(version=TOKEN_VERSION, query_hash='q' * 32,
                     device_handle=device_handle(device_id),
                     endpoints=(EndpointState(SEARCH_VIDEO_PATH, 30, 'SIDP', True, True),))


def _query(*, token: PageToken | None = None) -> SearchQuery:
    return SearchQuery(kind=SearchKind.KEYWORD, term='ocean', limit=20,
                       page_token=token)


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


def _fn(verdict, *, result='R', seen: list | None = None):
    """A `run_call` callable that returns `verdict` and records its client."""

    def call(client):  # noqa: ANN001
        if seen is not None:
            seen.append(client)
        return CallOutcome(result=result, verdict=verdict)

    return call


class TestNeutralReportsNeitherOkNorEmpty:
    """Property 1 — anti-block invariant (b).

    Every assertion here starts from a NON-ZERO consecutive-empty count. From
    zero, `report_ok` and no-report look identical, which is exactly how
    `NEUTRAL: True` survives a suite that only ever checks `== 0`."""

    def test_a_neutral_verdict_leaves_a_nonzero_empty_counter_untouched(
            self, tmp_path, monkeypatch):
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        store.report_empty(DEVICE_A)
        store.report_empty(DEVICE_A)
        assert store.get(DEVICE_A).consecutive_empty == ALMOST_STALE

        served, result = pool.run_call(_fn(HealthVerdict.NEUTRAL))

        assert result == 'R'
        assert served.handle == device_handle(DEVICE_A)
        # Neither incremented (no report_empty) nor CLEARED (no report_ok).
        assert store.get(DEVICE_A).consecutive_empty == ALMOST_STALE
        assert reports == []

    def test_neutral_does_not_stamp_last_ok(self, tmp_path):
        # `report_ok` also refreshes `last_ok`, so a call that proved nothing
        # must not make a cold identity look freshly proven.
        pool, store = _pool(tmp_path)
        store.report_empty(DEVICE_A)
        assert store.get(DEVICE_A).last_ok == 0.0

        pool.run_call(_fn(HealthVerdict.NEUTRAL))

        assert store.get(DEVICE_A).last_ok == 0.0
        assert store.get(DEVICE_A).consecutive_empty == 1

    def test_repeated_neutral_calls_neither_retire_nor_rescue_the_identity(
            self, tmp_path):
        # A dying identity parked one empty short of retirement: tails must not
        # walk it back, and must not push it over either.
        pool, store = _pool(tmp_path)
        for _ in range(ALMOST_STALE):
            store.report_empty(DEVICE_A)

        for _ in range(DEFAULT_STALE_AFTER + 2):
            pool.run_call(_fn(HealthVerdict.NEUTRAL))

        ident = store.get(DEVICE_A)
        assert ident.consecutive_empty == ALMOST_STALE
        assert not ident.stale
        # And the NEXT genuine empty still retires it on schedule — which is
        # the whole point: tails must not buy a dying identity extra lives.
        pool.run_call(_fn(HealthVerdict.EMPTY))
        assert store.get(DEVICE_A).consecutive_empty == DEFAULT_STALE_AFTER
        assert store.get(DEVICE_A).stale
        assert store.usable_count() == 0

    def test_a_continuation_tail_through_run_is_also_neutral_from_nonzero(
            self, tmp_path, monkeypatch):
        # The same property reached the way production reaches it: `run` →
        # `_search_verdict` → `run_call`, on an empty page with a page_token.
        pool, store = _pool(tmp_path)
        _slot(pool).client = FakeClient(DEVICE_A, _page([]))
        reports = _reports(pool, monkeypatch)
        store.report_empty(DEVICE_A)
        store.report_empty(DEVICE_A)

        served, page = pool.run(_query(token=_token()),
                                handle=device_handle(DEVICE_A))

        assert page.records == []
        assert served.handle == device_handle(DEVICE_A)
        assert store.get(DEVICE_A).consecutive_empty == ALMOST_STALE
        assert reports == []

    def test_a_continuation_with_records_still_clears_the_counter(
            self, tmp_path, monkeypatch):
        # Control on the same path: `_search_verdict`'s third branch. A page
        # WITH records is positive evidence and must report ok, so the NEUTRAL
        # exemption above is about EMPTINESS on a continuation and nothing more.
        pool, store = _pool(tmp_path)
        _slot(pool).client = FakeClient(DEVICE_A, _page([{'id': '1'}]))
        reports = _reports(pool, monkeypatch)
        for _ in range(ALMOST_STALE):
            store.report_empty(DEVICE_A)

        pool.run(_query(token=_token()), handle=device_handle(DEVICE_A))

        assert reports == [True]
        assert store.get(DEVICE_A).consecutive_empty == 0

    def test_ok_and_empty_are_both_visible_from_the_same_starting_point(
            self, tmp_path, monkeypatch):
        # Control for the two above: at ALMOST_STALE the counter really does
        # move in both directions, so "unchanged" is a measurement and not a
        # blind spot.
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        for _ in range(ALMOST_STALE):
            store.report_empty(DEVICE_A)

        pool.run_call(_fn(HealthVerdict.OK))
        assert store.get(DEVICE_A).consecutive_empty == 0
        assert reports == [True]

        for _ in range(ALMOST_STALE):
            store.report_empty(DEVICE_A)
        pool.run_call(_fn(HealthVerdict.EMPTY))
        assert store.get(DEVICE_A).consecutive_empty == DEFAULT_STALE_AFTER
        assert reports == [True, False]

    def test_the_table_maps_exactly_the_three_verdicts_and_neutral_to_none(self):
        # The table IS the rule; `run_call` only looks it up. `None` means "do
        # not report at all" and is not interchangeable with `False`.
        assert _REPORT_BY_VERDICT == {
            HealthVerdict.OK: True,
            HealthVerdict.EMPTY: False,
            HealthVerdict.NEUTRAL: None,
        }
        assert set(_REPORT_BY_VERDICT) == set(HealthVerdict)
        assert _REPORT_BY_VERDICT[HealthVerdict.NEUTRAL] is None


class TestAnUnrecognisedVerdictRaises:
    """Property 2. A verdict outside the table is a producer bug, and the
    table exists so it surfaces at once instead of defaulting to a wrong
    health report."""

    BOGUS = ('ok', None, True)

    def test_a_bogus_verdict_raises_and_reports_nothing(self, tmp_path, monkeypatch):
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        store.report_empty(DEVICE_A)

        for bogus in self.BOGUS:
            with pytest.raises(KeyError):
                pool.run_call(_fn(bogus))
            # No default was taken in either direction.
            assert reports == [], bogus
            assert store.get(DEVICE_A).consecutive_empty == 1, bogus
            assert not store.get(DEVICE_A).stale, bogus

    def test_a_bogus_verdict_still_releases_the_slot(self, tmp_path):
        # `run_call`'s `finally` owns release; a leaked slot would strand the
        # device for the life of the process.
        pool, _ = _pool(tmp_path)
        slot = _slot(pool)
        for bogus in self.BOGUS:
            with pytest.raises(KeyError):
                pool.run_call(_fn(bogus))
            assert not slot.inflight.locked(), bogus
        # And it really is servable again — a leak would raise BUSY here.
        pool.release(pool.acquire(handle=device_handle(DEVICE_A)))

    def test_the_verdict_enum_has_no_str_mixin_so_a_loose_string_cannot_match(self):
        # THE reason HealthVerdict is a plain Enum. With PoolCode's
        # `(str, Enum)` shape a bare `'ok'` compares AND hashes equal to the
        # member, so `_REPORT_BY_VERDICT['ok']` would silently succeed and a
        # caller returning a loose string would be accepted. Harmonising the
        # two enums is what this assertion forbids.
        assert not isinstance(HealthVerdict.OK, str)
        assert HealthVerdict.OK != 'ok'
        assert hash(HealthVerdict.OK) != hash('ok')
        assert 'ok' not in _REPORT_BY_VERDICT
        # The contrast, spelled out: PoolCode DOES carry the mixin on purpose,
        # because a code crosses the HTTP boundary as a string. A verdict never
        # does.
        assert isinstance(PoolCode.CAP, str)
        assert PoolCode.CAP == 'cap'
        assert hash(PoolCode.CAP) == hash('cap')


class TestCallOutcomeRequiresBothFields:
    """Property 3. Both fields required, no defaults, no mutation."""

    def test_a_missing_verdict_is_a_typeerror(self):
        with pytest.raises(TypeError):
            CallOutcome(result=1)

    def test_a_missing_result_is_a_typeerror(self):
        with pytest.raises(TypeError):
            CallOutcome(verdict=HealthVerdict.OK)

    def test_neither_field_carries_a_default(self):
        fields = {f.name: f for f in dataclasses.fields(CallOutcome)}
        assert set(fields) == {'result', 'verdict'}
        for field in fields.values():
            assert field.default is dataclasses.MISSING, field.name
            assert field.default_factory is dataclasses.MISSING, field.name

    def test_the_outcome_is_frozen_and_slotted(self):
        outcome = CallOutcome(result=1, verdict=HealthVerdict.OK)
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.verdict = HealthVerdict.EMPTY
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.result = 2
        assert CallOutcome.__slots__ == ('result', 'verdict')
        assert not hasattr(outcome, '__dict__')


class TestOnlySoftErrorReportsAnything:
    """Property 5. Every exception path, measured from a NON-ZERO counter so a
    stray `report_ok` is as visible as a stray `report_empty`."""

    def _raising(self, exc):
        def call(client):  # noqa: ANN001
            raise exc
        return call

    def test_a_softerror_is_reported_as_empty_whatever_fn_would_have_said(
            self, tmp_path, monkeypatch):
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        for _ in range(ALMOST_STALE):
            store.report_empty(DEVICE_A)

        with pytest.raises(SoftError):
            pool.run_call(self._raising(SoftError('empty')))

        assert reports == [False]
        assert store.get(DEVICE_A).consecutive_empty == DEFAULT_STALE_AFTER
        assert store.get(DEVICE_A).stale

    def test_no_other_exception_class_reports_anything(self, tmp_path, monkeypatch):
        # NotFound / TransportError / ValueError each pass through `run_call`
        # invisible to identity health. Widening the `except` to `Exception`
        # would charge all three.
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        store.report_empty(DEVICE_A)
        slot = _slot(pool)

        for exc in (NotFound('no such user'), TransportError('odd payload'),
                    ValueError('producer bug')):
            with pytest.raises(type(exc)):
                pool.run_call(self._raising(exc))
            assert reports == [], exc
            assert store.get(DEVICE_A).consecutive_empty == 1, exc
            assert not slot.inflight.locked(), exc

        assert not issubclass(NotFound, SoftError)
        assert not issubclass(TransportError, SoftError)

    def test_a_reported_softerror_still_releases_the_slot(self, tmp_path):
        pool, _ = _pool(tmp_path)
        slot = _slot(pool)
        with pytest.raises(SoftError):
            pool.run_call(self._raising(SoftError('empty')))
        assert not slot.inflight.locked()


class TestRunCallHonoursThePinAndTheCap:
    """Property 4. The pin and the daily-cap reservation are `run_call`'s, not
    `run`'s — every pinned outcome is reached here through `run_call` directly."""

    def test_the_pin_selects_the_pinned_client(self, tmp_path):
        pool, _ = _pool(tmp_path, [identity(DEVICE_A), identity(DEVICE_B)])
        seen: list = []

        served, _result = pool.run_call(_fn(HealthVerdict.OK, seen=seen),
                                        handle=device_handle(DEVICE_B))

        assert [c.device_id for c in seen] == [DEVICE_B]
        assert served.handle == device_handle(DEVICE_B)

    def test_an_unpinned_call_still_serves_and_reports(self, tmp_path, monkeypatch):
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        served, result = pool.run_call(_fn(HealthVerdict.OK, result='X'))
        assert result == 'X'
        assert served.label == 'id0'
        assert reports == [True]
        assert store.get(DEVICE_A).last_ok > 0.0

    def test_one_run_call_consumes_exactly_one_cap_unit(self, tmp_path):
        pool, _ = _pool(tmp_path, daily_request_cap_per_device=5)
        slot = _slot(pool)
        assert slot.remaining() == 5

        pool.run_call(_fn(HealthVerdict.OK))
        assert slot.remaining() == 4
        pool.run_call(_fn(HealthVerdict.NEUTRAL), handle=device_handle(DEVICE_A))
        assert slot.remaining() == 3

    def test_the_cap_is_charged_per_run_call_not_per_signed_request(
            self, tmp_path, transport):
        # The plan's cost model, made visible: the reservation happens once, in
        # `acquire`. An `fn` that issues SEVERAL signed requests still spends
        # ONE cap unit — a second endpoint stacking two requests into one `fn`
        # would silently undercount the device's daily budget.
        pool, _ = _pool(tmp_path, daily_request_cap_per_device=5)
        slot = _slot(pool)
        transport.script(lambda call: reply([1], cursor=20, has_more=False))

        signed: list[int] = []

        def two_searches(client):  # noqa: ANN001
            client.search(_query())
            signed.append(len(transport.calls))
            client.search(_query())
            signed.append(len(transport.calls))
            return CallOutcome(result=None, verdict=HealthVerdict.OK)

        pool.run_call(two_searches)

        # The second search really did sign more requests …
        assert signed[0] >= 1 and signed[1] > signed[0]
        # … and the whole `run_call` still cost exactly one cap unit.
        assert slot.remaining() == 4

    def test_a_vanished_pin_is_gone_and_never_invokes_fn(self, tmp_path):
        pool, _ = _pool(tmp_path)
        seen: list = []
        with pytest.raises(PoolExhausted) as excinfo:
            pool.run_call(_fn(HealthVerdict.OK, seen=seen),
                          handle=device_handle(DEVICE_B))
        assert excinfo.value.code is PoolCode.GONE
        assert seen == []

    def test_a_capped_pin_is_cap_and_never_invokes_fn(self, tmp_path):
        pool, _ = _pool(tmp_path, daily_request_cap_per_device=1)
        seen: list = []
        handle = device_handle(DEVICE_A)
        pool.run_call(_fn(HealthVerdict.OK, seen=seen), handle=handle)
        with pytest.raises(PoolExhausted) as excinfo:
            pool.run_call(_fn(HealthVerdict.OK, seen=seen), handle=handle)
        assert excinfo.value.code is PoolCode.CAP
        assert len(seen) == 1

    def test_a_stale_pin_is_stale_and_never_invokes_fn(self, tmp_path, monkeypatch):
        pool, store = _pool(tmp_path)
        reports = _reports(pool, monkeypatch)
        for _ in range(DEFAULT_STALE_AFTER):
            store.report_empty(DEVICE_A)
        seen: list = []

        with pytest.raises(PoolExhausted) as excinfo:
            pool.run_call(_fn(HealthVerdict.OK, seen=seen),
                          handle=device_handle(DEVICE_A))

        assert excinfo.value.code is PoolCode.STALE
        assert seen == []
        # A refused acquire must not report anything either: the identity is
        # already retired and `fn` never ran.
        assert reports == []

    def test_a_refused_pin_costs_no_cap_unit(self, tmp_path):
        pool, _ = _pool(tmp_path, daily_request_cap_per_device=5)
        slot = _slot(pool)
        with pytest.raises(PoolExhausted):
            pool.run_call(_fn(HealthVerdict.OK), handle=device_handle(DEVICE_B))
        assert slot.remaining() == 5
