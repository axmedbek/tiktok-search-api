"""Stubbed wiring/contract checks for the per-call `PayloadShape` + `NotFound`
seam (Epic subtask 2).

These are COMPOSITION checks, not mapper unit tests: each one drives the real
`_get_signed` retry loop, or the real `pool.run` → `client.search` →
`_get_signed` chain, over `FakeTransport`. Nothing leaves the process — the
session-wide `HTTPAdapter.send` tripwire in conftest still holds, and the paid
signer is asserted on its CONSTRUCTION ledger (`rapid_ledger`) rather than on
which exception came out, because swapping signers on an empty result is what
`.claude/rules/lessons/anti-block.md` is organised against.

Five properties, all of them load-bearing for the profile/posts endpoints that
land in later subtasks:

1. `NotFound` is invisible to `pool.run`'s identity-health accounting, and the
   slot is still released.
2. `NotFound` costs exactly ONE signed request — it must not spend the retry
   budget re-signing for a user that does not exist.
3. The new branch is DEAD on the search path: `SEARCH_PAYLOAD` carries an empty
   `not_found_statuses`, so a non-zero `status_code` on a search is still a
   retried `SoftError` charged to identity health.
4. The paid-signer fallback is unreachable from an empty result or from the
   `NotFound` branch — it is reached only from `_sign`'s `TransportError`.
5. The `has_session` tail branch is gated on `shape.session_aware`, so a posts
   reply that DROPPED its payload cannot be laundered into a silent tail 200.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    STUB_SIGNER_KEY,
    FakeTransport,
    identity,
    local_config,
    write_identities,
)

from tiktoksearch.client import (  # noqa: E402
    NO_SUCH_USER_STATUSES,
    _has_search_items,
    POSTS_PAYLOAD,
    PROFILE_PAYLOAD,
    SEARCH_PAYLOAD,
    SEARCH_USER_PATH,
    TikTokClient,
)
from tiktoksearch.config import PoolConfig  # noqa: E402
from tiktoksearch.errors import NotFound, SoftError, TikTokSearchError  # noqa: E402
from tiktoksearch.filters import SearchKind, SearchQuery  # noqa: E402
from tiktoksearch.identity_manager import IdentityStore  # noqa: E402
from tiktoksearch.paging import device_handle  # noqa: E402
from tiktoksearch.pool import ClientPool  # noqa: E402

DEVICE_A = 'DEVA'
# The two upstream paths land as consts in subtask 3. `_get_signed` takes the
# path as a plain string and the SHAPE is what is under test here, so these
# stand in without pre-empting that subtask's naming.
PROFILE_PATH = '/aweme/v1/user/profile/other/'
POSTS_PATH = '/aweme/v1/aweme/post/'
IDS = {'user_id': '9', 'sec_user_id': 'SEC-9'}


def _client(**over) -> TikTokClient:
    """A warm `signer: local` client. Real in-process signing, no socket."""
    return TikTokClient(local_config(**{'retries': 2, **over}))


def _pool(tmp_path, **config_over) -> tuple[ClientPool, IdentityStore]:
    """A one-identity pool over a synthetic identities file."""
    path = tmp_path / 'ids.json'
    write_identities(path, [identity(DEVICE_A)], stamp=1_700_000_000)
    mapping = {'rapidapi_key': STUB_SIGNER_KEY, 'acquire_timeout_s': 0.05,
               'signer': 'local'}
    mapping.update(config_over)
    store = IdentityStore(path)
    return ClientPool(PoolConfig.from_mapping(mapping), identities=store), store


def _slot(pool: ClientPool):
    return next(s for s in pool._slots if s.client.device_id == DEVICE_A)


class RaisingClient:
    """Stands in for TikTokClient inside a slot: raises instead of searching."""

    proxy = None

    def __init__(self, exc: BaseException) -> None:
        self.device_id = DEVICE_A
        self.iid = f'IID-{DEVICE_A}'
        self._exc = exc

    def search(self, query: SearchQuery):
        raise self._exc


def _query(kind: str = SearchKind.KEYWORD) -> SearchQuery:
    return SearchQuery(kind=kind, term='ocean', limit=10)


class TestNotFoundIsInvisibleToIdentityHealth:
    """Property 1. `pool.run` penalises only `SoftError` and an empty first
    page. `NotFound` must pass through untouched — a username the caller made
    up is not evidence against a warm identity, and three of them would
    otherwise retire the only usable one and 503 every caller."""

    def test_notfound_is_not_a_softerror_subclass(self):
        # The structural fact the whole property rests on: `pool.run`'s
        # `except SoftError` cannot see this, whatever a later edit does to
        # the handler body.
        assert not issubclass(NotFound, SoftError)
        assert issubclass(NotFound, TikTokSearchError)

    def test_notfound_neither_penalises_nor_clears_the_empty_counter(self, tmp_path):
        pool, store = _pool(tmp_path)
        _slot(pool).client = RaisingClient(NotFound('no such user'))
        store.report_empty(DEVICE_A)          # a pre-existing, real penalty
        assert store.get(DEVICE_A).consecutive_empty == 1

        with pytest.raises(NotFound):
            pool.run(_query())
        # Not incremented (no report_empty) and not reset (no report_ok).
        assert store.get(DEVICE_A).consecutive_empty == 1

    def test_the_slot_is_released_so_the_device_stays_servable(self, tmp_path):
        pool, _ = _pool(tmp_path)
        slot = _slot(pool)
        slot.client = RaisingClient(NotFound('no such user'))
        with pytest.raises(NotFound):
            pool.run(_query())
        assert not slot.inflight.locked()
        # And it really can be acquired again — a leaked slot would raise BUSY.
        pool.release(pool.acquire(handle=device_handle(DEVICE_A)))

    def test_a_softerror_on_the_same_path_still_is_charged(self, tmp_path):
        # Control: the bypass is specific to NotFound, not a broken handler.
        pool, store = _pool(tmp_path)
        _slot(pool).client = RaisingClient(SoftError('empty'))
        with pytest.raises(SoftError):
            pool.run(_query())
        assert store.get(DEVICE_A).consecutive_empty == 1


class TestNotFoundCostsOneSignedRequest:
    """Property 2. It is raised from INSIDE the retry loop, so the assertion
    has to be on the transport ledger: re-signing cannot make a deleted user
    exist, and every extra attempt is another daily-cap unit."""

    @pytest.mark.parametrize('status', sorted(NO_SUCH_USER_STATUSES))
    def test_a_profile_notfound_signs_exactly_once(
        self, transport: FakeTransport, status: int
    ):
        transport.script(lambda call: {'status_code': status, 'message': 'user not exist'})
        client = _client(retries=2)
        with pytest.raises(NotFound):
            client._get_signed(PROFILE_PATH, dict(IDS), shape=PROFILE_PAYLOAD)
        assert len(transport.calls) == 1, transport.paths

    @pytest.mark.parametrize('status', sorted(NO_SUCH_USER_STATUSES))
    def test_a_posts_notfound_signs_exactly_once(
        self, transport: FakeTransport, status: int
    ):
        transport.script(lambda call: {'status_code': status, 'message': 'user not exist'})
        with pytest.raises(NotFound):
            _client(retries=2)._get_signed(POSTS_PATH, dict(IDS), shape=POSTS_PAYLOAD)
        assert len(transport.calls) == 1, transport.paths

    def test_an_unknown_nonzero_status_still_spends_the_full_budget(
        self, transport: FakeTransport
    ):
        # The narrowness cuts both ways: only the allow-listed statuses skip
        # retries. Anything else is an ordinary upstream soft error.
        assert 9999 not in NO_SUCH_USER_STATUSES
        transport.script(lambda call: {'status_code': 9999, 'message': 'boom'})
        with pytest.raises(SoftError):
            _client(retries=2)._get_signed(PROFILE_PATH, dict(IDS), shape=PROFILE_PAYLOAD)
        assert len(transport.calls) == 3

    def test_the_notfound_message_carries_no_upstream_text(
        self, transport: FakeTransport
    ):
        # It reaches the HTTP client as a 404 body, so it must not echo TikTok.
        transport.script(lambda call: {'status_code': 2053,
                                       'message': 'user 1234 has been banned'})
        with pytest.raises(NotFound) as excinfo:
            _client()._get_signed(PROFILE_PATH, dict(IDS), shape=PROFILE_PAYLOAD)
        assert 'banned' not in str(excinfo.value)
        assert '1234' not in str(excinfo.value)


class TestTheNotFoundBranchIsDeadOnSearch:
    """Property 3. `SEARCH_PAYLOAD.not_found_statuses` is empty, so search
    behaviour on a non-zero `status_code` is bit-for-bit what it was: retried,
    raised as `SoftError`, and charged to identity health. No test in the suite
    carried a non-zero status before this one."""

    def test_the_search_shape_allow_lists_nothing(self):
        assert SEARCH_PAYLOAD.not_found_statuses == frozenset()
        assert SEARCH_PAYLOAD.session_aware is True

    def test_every_field_of_the_search_shape_is_pinned(self):
        # The FULL shape, field by field, because `/search`'s classification is
        # now data rather than code: an edit to any one of these five values
        # re-classifies every search reply, and four of the five would do it
        # SILENTLY (the suite would stay green while `/search` started 502ing
        # tails, or laundering shadow-blocks into empty 200s). Pinned here so
        # such an edit is a failing test rather than a production incident.
        assert SEARCH_PAYLOAD.name == 'search'
        assert SEARCH_PAYLOAD.has_payload is _has_search_items
        assert SEARCH_PAYLOAD.empty_reason == 'no items, has_more=false (shadow-block)'
        assert SEARCH_PAYLOAD.session_aware is True
        assert SEARCH_PAYLOAD.not_found_statuses == frozenset()

    def test_the_search_shape_is_frozen(self):
        # `PayloadShape` is a frozen dataclass, so nothing can retune the
        # search rule in place at runtime — the classification a caller gets
        # is the one this module declares.
        with pytest.raises(FrozenInstanceError):
            SEARCH_PAYLOAD.session_aware = False   # type: ignore[misc]

    def test_the_empty_reason_text_reaches_the_softerror_message(self, transport):
        # The value is not decoration: it is the diagnostic an operator greps
        # when `/search` starts coming back empty, and it is what distinguishes
        # a shadow-block from a nil-coded one. Asserted on the raised message,
        # so a shape whose `empty_reason` changed cannot pass by having merely
        # been renamed in the constant.
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'data': []})
        client = _client(retries=0)
        with pytest.raises(SoftError) as excinfo:
            client._get_signed(SEARCH_USER_PATH, {'keyword': 'ocean'},
                               shape=SEARCH_PAYLOAD)
        assert str(excinfo.value) == (
            'empty search result (no items, has_more=false (shadow-block))')

    def test_a_nil_code_replaces_the_empty_reason_rather_than_joining_it(
        self, transport
    ):
        # The other half of the same branch: when TikTok names the reason
        # itself, ITS name is reported and the generic fallback text is not.
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'data': [],
                                       'search_nil_info': {'search_nil_item': 'empty_session'}})
        client = _client(retries=0)
        with pytest.raises(SoftError) as excinfo:
            client._get_signed(SEARCH_USER_PATH, {'keyword': 'ocean'},
                               shape=SEARCH_PAYLOAD)
        assert str(excinfo.value) == 'empty search result (empty_session)'
        assert 'shadow-block' not in str(excinfo.value)

    @pytest.mark.parametrize('shape,name,reason', [
        (PROFILE_PAYLOAD, 'profile', 'no user object (shadow-block)'),
        (POSTS_PAYLOAD, 'posts', 'no aweme_list key (shadow-block)'),
    ])
    def test_the_two_new_shapes_name_themselves_distinguishably(
        self, shape, name, reason
    ):
        # Three distinct `name`s and three distinct `empty_reason`s, so a
        # WARNING line and a 502 body say which endpoint went empty. Collapsed
        # to one shared string, an operator cannot tell a dead profile call
        # from a dead search from the log.
        assert shape.name == name
        assert shape.empty_reason == reason
        assert shape.name != SEARCH_PAYLOAD.name
        assert shape.empty_reason != SEARCH_PAYLOAD.empty_reason
        assert shape.not_found_statuses == NO_SUCH_USER_STATUSES

    @pytest.mark.parametrize('status', sorted(NO_SUCH_USER_STATUSES))
    def test_a_no_such_user_status_on_a_search_is_a_retried_softerror(
        self, transport: FakeTransport, status: int
    ):
        transport.script(lambda call: {'status_code': status, 'message': 'nope'})
        client = _client(retries=2)
        with pytest.raises(SoftError):
            client._get_signed(SEARCH_USER_PATH, {'keyword': 'ocean'},
                               shape=SEARCH_PAYLOAD)
        assert len(transport.calls) == 3, 'the retry budget must still be spent'

    def test_a_search_status_error_is_charged_to_identity_health(self, tmp_path, transport):
        # End to end through the real chain: pool.run → client.search →
        # _get_signed → FakeTransport. A user search drives ONE endpoint.
        transport.script(lambda call: {'status_code': 2053, 'message': 'nope'})
        pool, store = _pool(tmp_path, retries=1)
        with pytest.raises(SoftError):
            pool.run(_query(SearchKind.USER))
        assert store.get(DEVICE_A).consecutive_empty == 1
        assert transport.calls, 'nothing was requested, so nothing was classified'

    def test_a_search_status_error_never_escapes_as_notfound(self, transport, tmp_path):
        transport.script(lambda call: {'status_code': 2053, 'message': 'nope'})
        pool, _ = _pool(tmp_path, retries=1)
        with pytest.raises(SoftError) as excinfo:
            pool.run(_query(SearchKind.USER))
        assert not isinstance(excinfo.value, NotFound)


class TestTheFallbackStaysUnreachableFromEmptyAndNotFound:
    """Property 4. The paid signer answers a SIGNING failure, never an empty or
    a missing user. The assertion is the receipt: 0 constructions."""

    def test_a_profile_notfound_builds_no_paid_signer(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        transport.script(lambda call: {'status_code': 2053, 'message': 'nope'})
        client = _client(rapidapi_key=STUB_SIGNER_KEY)
        with pytest.raises(NotFound):
            client._get_signed(PROFILE_PATH, dict(IDS), shape=PROFILE_PAYLOAD)
        assert rapid_ledger == []
        assert client._fallback is None

    @pytest.mark.parametrize('shape,path,payload', [
        (PROFILE_PAYLOAD, PROFILE_PATH, {'status_code': 0, 'user': {}}),
        (POSTS_PAYLOAD, POSTS_PATH, {'status_code': 0, 'has_more': False}),
    ])
    def test_a_full_retry_run_of_shape_empties_builds_no_paid_signer(
        self, transport: FakeTransport, rapid_ledger: list, shape, path, payload
    ):
        transport.script(lambda call: dict(payload))
        client = _client(rapidapi_key=STUB_SIGNER_KEY, retries=2)
        with pytest.raises(SoftError):
            client._get_signed(path, dict(IDS), shape=shape)
        assert len(transport.calls) == 3
        assert rapid_ledger == []
        assert client._fallback is None


class TestTheTailBranchIsGatedOnSessionAwareness:
    """Property 5. `has_session` alone must not open the tail branch: a posts
    reply that dropped `aweme_list` entirely is risk-control, and returning it
    as an empty page would hand the caller a silent 200 AND leave IdentityStore
    blind, because `pool.run` reports neither ok nor empty on a continuation."""

    def test_only_the_search_shape_is_session_aware(self):
        assert PROFILE_PAYLOAD.session_aware is False
        assert POSTS_PAYLOAD.session_aware is False

    def test_a_posts_reply_missing_its_payload_raises_even_with_a_session(
        self, transport: FakeTransport
    ):
        # `has_more: True` and a tail-shaped absence of records is exactly the
        # combination the search tail branch would have swallowed.
        transport.script(lambda call: {'status_code': 0, 'has_more': True,
                                       'max_cursor': 1_780_000_000_000})
        client = _client(retries=2)
        with pytest.raises(SoftError) as excinfo:
            client._get_signed(POSTS_PATH, dict(IDS), shape=POSTS_PAYLOAD,
                               has_session=True)
        assert 'posts' in str(excinfo.value)
        assert len(transport.calls) == 3, 'a shadow-block is retried, not returned'

    def test_a_posts_reply_missing_its_payload_raises_with_a_tail_nil_present(
        self, transport: FakeTransport
    ):
        # `search_nil_info` is a search concept. Even an ALLOW-LISTED tail nil
        # must not open the tail branch on a non-session-aware shape.
        transport.script(lambda call: {
            'status_code': 0, 'has_more': False,
            'search_nil_info': {'search_nil_item': 'empty_session'}})
        with pytest.raises(SoftError):
            _client(retries=1)._get_signed(POSTS_PATH, dict(IDS),
                                           shape=POSTS_PAYLOAD, has_session=True)

    def test_an_empty_but_present_aweme_list_is_an_ordinary_page(
        self, transport: FakeTransport
    ):
        # The other direction: a private / zero-post account is a 200 with zero
        # records, not a 502, and its `has_more` is NOT normalised to False by
        # the search tail branch.
        transport.script(lambda call: {'status_code': 0, 'aweme_list': [],
                                       'has_more': True, 'max_cursor': 17})
        data = _client(retries=2)._get_signed(POSTS_PATH, dict(IDS),
                                             shape=POSTS_PAYLOAD, has_session=True)
        assert data['aweme_list'] == []
        assert data['has_more'] is True
        assert len(transport.calls) == 1, 'a good reply is not retried'

    def test_a_populated_profile_reply_is_not_a_shadow_block(
        self, transport: FakeTransport
    ):
        # Finding 1 of the plan, as a wiring check: the search rule applied to
        # this reply (no item list, no has_more) 502'd every good profile.
        transport.script(lambda call: {'status_code': 0,
                                       'user': {'uid': '9', 'unique_id': 'u'}})
        data = _client()._get_signed(PROFILE_PATH, dict(IDS), shape=PROFILE_PAYLOAD)
        assert data['user']['uid'] == '9'
        assert len(transport.calls) == 1

    def test_the_search_tail_branch_still_works(self, transport: FakeTransport):
        # Control: gating the branch did not disable it for the shape that
        # needs it. A session-carrying empty search is a tail, normalised to
        # has_more=False so the published EndpointState is not resumable.
        transport.script(lambda call: {'status_code': 0, 'has_more': True,
                                       'cursor': 30, 'data': [],
                                       'search_nil_info':
                                           {'search_nil_item': 'empty_session'}})
        data = _client()._get_signed(SEARCH_USER_PATH, {'keyword': 'ocean'},
                                     shape=SEARCH_PAYLOAD, has_session=True)
        assert data['has_more'] is False
        assert len(transport.calls) == 1
