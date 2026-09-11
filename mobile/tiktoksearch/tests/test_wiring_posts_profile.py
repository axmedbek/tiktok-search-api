"""Stubbed wiring/contract checks for `resolve_user` / `profile` /
`user_posts` / `_paginate_posts` (Epic subtask 3).

COMPOSITION checks, not mapper unit tests: every one drives the real
`_get_signed` retry loop over `FakeTransport`, so the signed-request LEDGER
(`transport.calls`) and the paid-signer CONSTRUCTION ledger (`rapid_ledger`)
are the evidence, not merely which exception came out. Nothing leaves the
process — conftest's session-wide `HTTPAdapter.send` tripwire still holds.

These properties are HOST-INDEPENDENT: which upstream host eventually serves
`/aweme/v1/user/profile/other/` and `/aweme/v1/aweme/post/` is a separate,
live question. Everything asserted here is the client's own classification and
cursor logic, which stands whichever host answers.

Eight properties:

1. `_paginate_posts`' terminal ordering cannot publish a resumable state with
   an unmoved cursor. `has_more=True` escapes ONLY with a strictly-advanced
   cursor — checked at each of the five exits. Trusting `has_more: true` on a
   reply already classified terminal is what produced an unbounded chain of
   signed requests at one paid signature each
   (`.claude/rules/lessons/anti-block.md`).
2. Backwards-cursor boundaries. A posts `max_cursor` is a millisecond epoch
   walking DOWN, so "progress" is STRICTLY SMALLER — except on the first page,
   where any positive cursor is progress. A normal last page must NOT be
   logged as a non-advance anomaly.
3. Backwards chaining across pages: descending cursors accumulate, dedupe
   through the token-seeded `SeenWindow`, and publish an `EndpointState` whose
   `search_id` is ALWAYS `''` — a posts stream has no search session.
4. `user_posts` on an exhausted seed state signs ZERO requests.
5. `resolve_user`'s four branches, and its cost in signed requests.
6. `profile`'s identity-health safety: a `uid`-less `user` raises
   `TransportError`, NOT `SoftError`. The exception CLASS is the assertion —
   `pool.run` reports an empty only inside `except SoftError`, so the whole
   "an odd payload costs the warm identity nothing" argument is that class.
7. Paid-signer construction ledger is 0 on every path above.
8. `_user_search_params` regression pin: `/search`'s user path sends the exact
   dict it sent before the extraction, so the signed URL cannot drift.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    STUB_SIGNER_KEY,
    FakeTransport,
    local_config,
    posts_reply,
    users,
)

from tiktoksearch.client import (  # noqa: E402
    POSTS_PAGE_COUNT,
    POSTS_START_CURSOR,
    RESOLVE_USER_COUNT,
    SEARCH_USER_PATH,
    USER_POSTS_PATH,
    USER_PROFILE_PATH,
    TikTokClient,
    _page_budget,
    _user_search_params,
)
from tiktoksearch.errors import NotFound, SoftError, TransportError  # noqa: E402
from tiktoksearch.filters import SearchKind, SearchQuery  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    EndpointState,
    PageToken,
    SeenWindow,
    fingerprint,
)

LOGGER = 'tiktoksearch.client'
# Millisecond-epoch cursors, descending — the shape a posts reply answers with.
# Deliberately far above `paging.MAX_ENDPOINT_CURSOR` (100_000): the search
# bound must not be applied on this path.
MS = 1_700_000_000_000
UID = '9'
SEC = 'SEC-9'
WANTED = 'bakuesaz'


def _client(**over) -> TikTokClient:
    """A warm `signer: local` client. Real in-process signing, no socket."""
    return TikTokClient(local_config(**{'retries': 2, **over}))


def _script(transport: FakeTransport, pages: list[dict]) -> None:
    """Answer each signed request with the next scripted page. Running off the
    end is an assertion failure, not a silent repeat: an over-long chain is
    exactly the defect these tests exist to catch."""
    remaining = list(pages)

    def handler(call):  # noqa: ANN001
        assert remaining, f'the loop asked for more pages than scripted: {call["path"]}'
        return remaining.pop(0)

    transport.script(handler)


def _assert_resumable_only_if_advanced(end, start_cursor: int) -> None:
    """Property 1's invariant, in one place: a published `has_more=True` must
    come with a cursor that STRICTLY moved in the backwards direction (or, from
    the start marker, moved at all)."""
    if not end.has_more:
        return
    assert end.cursor > POSTS_START_CURSOR, (
        f'published has_more=True at cursor {end.cursor} — a resumable state '
        'must never carry the start marker')
    if start_cursor != POSTS_START_CURSOR:
        assert end.cursor < start_cursor, (
            f'published has_more=True with a cursor that did not walk back '
            f'({start_cursor} -> {end.cursor})')


def _paginate(client: TikTokClient, *, limit: int, start_cursor: int,
              seen: SeenWindow | None = None) -> tuple[list[dict], object]:
    out: list[dict] = []
    end = client._paginate_posts(
        out=out, seen=seen if seen is not None else SeenWindow(),
        user_id=UID, limit=limit, start_cursor=start_cursor)
    _assert_resumable_only_if_advanced(end, start_cursor)
    # Property 3, pinned at the INNER seam as well as at the published state:
    # every scripted reply carries `log_pb.impr_id`, so a search_id read here
    # would surface. `user_posts` hardcoding '' must not be the only guard.
    assert end.search_id == '', 'a posts stream has no search session'
    return out, end


def _posts_token(*, cursor: int, has_more: bool, seen=()) -> PageToken:
    """A page token carrying only a posts endpoint entry. Built directly rather
    than through `encode`/`decode`: `_posts_seed` reads `state_for`, and the
    token's HMAC is `paging`'s own tested property, not this seam's."""
    return PageToken(version=1, query_hash='q' * 32, device_handle='d' * 16,
                     endpoints=(EndpointState(path=USER_POSTS_PATH, cursor=cursor,
                                              search_id='', has_more=has_more,
                                              started=True),),
                     seen=tuple(seen))


class TestPostsTerminalOrdering:
    """Property 1. The ordering is `not has_more` -> `not advanced` (warns,
    retires) -> `not raw_items` (stays resumable). Each of the five exits is
    driven, and each one is checked against the single invariant above."""

    def test_budget_exit_stays_resumable_on_an_advanced_cursor(
        self, transport: FakeTransport
    ):
        budget = _page_budget(100, POSTS_PAGE_COUNT)
        assert budget == 9, 'the arithmetic this test depends on'
        pages = [posts_reply([i], max_cursor=MS - i * 1000, has_more=True)
                 for i in range(1, budget + 1)]
        _script(transport, pages)
        out, end = _paginate(_client(), limit=100, start_cursor=POSTS_START_CURSOR)
        # Ends THIS request only: the endpoint is still resumable, and the
        # cursor it publishes is the last page's, strictly walked back.
        assert len(transport.calls) == budget
        assert len(out) == budget
        assert end.has_more is True
        assert end.cursor == MS - budget * 1000

    def test_not_has_more_retires_without_the_non_advance_warning(
        self, transport: FakeTransport, caplog
    ):
        # TikTok's own end of stream, answered the way a real last page is:
        # has_more=false AND max_cursor=0. The ordering exists precisely so
        # this ordinary ending is not logged as an anomaly.
        _script(transport, [posts_reply([1], max_cursor=0, has_more=False)])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            out, end = _paginate(_client(), limit=10,
                                 start_cursor=POSTS_START_CURSOR)
        assert len(transport.calls) == 1
        assert len(out) == 1
        assert end.has_more is False
        assert 'did not advance' not in caplog.text

    def test_a_non_advancing_cursor_warns_and_retires(
        self, transport: FakeTransport, caplog
    ):
        _script(transport, [
            posts_reply([1], max_cursor=MS, has_more=True),
            # Same cursor echoed back, still promising more: without the guard
            # this is an unbounded chain of signed requests.
            posts_reply([2], max_cursor=MS, has_more=True),
        ])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            out, end = _paginate(_client(), limit=50,
                                 start_cursor=POSTS_START_CURSOR)
        assert len(transport.calls) == 2
        assert len(out) == 2, 'records already served are kept'
        assert end.has_more is False, 'a stuck cursor is not resumable'
        assert end.cursor == MS
        assert 'did not advance' in caplog.text

    def test_an_empty_mid_stream_page_stays_resumable(self, transport: FakeTransport):
        # `not raw_items` is LAST: the cursor did advance and TikTok still says
        # has_more, so the endpoint is resumable from the advanced cursor.
        _script(transport, [
            posts_reply([1], max_cursor=MS, has_more=True),
            posts_reply([], max_cursor=MS - 1000, has_more=True),
        ])
        out, end = _paginate(_client(), limit=50, start_cursor=POSTS_START_CURSOR)
        assert len(transport.calls) == 2
        assert len(out) == 1
        assert end.has_more is True
        assert end.cursor == MS - 1000

    def test_the_limit_exit_publishes_the_advanced_cursor(
        self, transport: FakeTransport
    ):
        _script(transport, [posts_reply([1, 2, 3], max_cursor=MS, has_more=True)])
        out, end = _paginate(_client(), limit=2, start_cursor=POSTS_START_CURSOR)
        assert len(transport.calls) == 1
        assert [r['id'] for r in out] == ['1', '2'], 'truncated at limit'
        assert end.has_more is True
        assert end.cursor == MS

    def test_a_limit_filling_page_with_a_stuck_cursor_still_retires(
        self, transport: FakeTransport
    ):
        # The dangerous interleaving: the inner record loop breaks on `limit`
        # BEFORE the terminal checks run, so a page that both fills the limit
        # and fails to advance must still be retired rather than published as
        # resumable off its own `has_more: true`.
        _script(transport, [
            posts_reply([1], max_cursor=MS, has_more=True),
            posts_reply([2, 3], max_cursor=MS, has_more=True),
        ])
        out, end = _paginate(_client(), limit=2, start_cursor=POSTS_START_CURSOR)
        assert len(out) == 2
        assert end.has_more is False
        assert end.cursor == MS


class TestBackwardsCursorBoundaries:
    """Property 2. `advanced = next_cursor > 0 and (prev == start_marker or
    next_cursor < prev)`. Each clause gets its own case."""

    def test_the_first_page_accepts_any_positive_cursor(self, transport: FakeTransport):
        # From the start marker there is no "smaller" to require: a ms-epoch
        # cursor is enormously larger than 0 and is still progress. It is also
        # far past `paging.MAX_ENDPOINT_CURSOR`, which must not be applied here.
        _script(transport, [posts_reply([1], max_cursor=MS, has_more=True),
                            posts_reply([2], max_cursor=MS - 1, has_more=False)])
        out, end = _paginate(_client(), limit=50, start_cursor=POSTS_START_CURSOR)
        assert [c['params']['max_cursor'] for c in transport.calls] == ['0', str(MS)]
        assert len(out) == 2
        assert end.cursor == MS - 1

    @pytest.mark.parametrize('echoed,label', [
        (MS + 1000, 'forwards'),
        (MS, 'unmoved'),
        (0, 'reset to the start marker'),
        (None, 'absent'),
    ])
    def test_a_resumed_page_requires_a_strictly_smaller_cursor(
        self, transport: FakeTransport, echoed, label
    ):
        _script(transport, [posts_reply([1], max_cursor=echoed, has_more=True,
                                        omit_list=False)])
        out, end = _paginate(_client(), limit=50, start_cursor=MS)
        assert len(transport.calls) == 1, f'{label}: retired in one request'
        assert len(out) == 1, 'the records this page served are kept'
        assert end.has_more is False, f'{label} is not progress'
        assert end.cursor == MS, 'the stored cursor is not moved by a bad echo'

    def test_the_resumed_page_sends_the_seed_cursor(self, transport: FakeTransport):
        _script(transport, [posts_reply([1], max_cursor=MS - 1000, has_more=False)])
        _paginate(_client(), limit=50, start_cursor=MS)
        sent = transport.calls[0]['params']
        assert sent['max_cursor'] == str(MS)
        assert sent['user_id'] == UID
        # NO sec_user_id: the app's own working request omits it and an
        # unvalidated one was measured to get the request refused with a
        # zero-byte body (2026-09-11, Arm M).
        assert 'sec_user_id' not in sent
        assert sent['count'] == str(POSTS_PAGE_COUNT)
        assert transport.calls[0]['path'] == USER_POSTS_PATH


class TestBackwardsChainingAcrossPages:
    """Property 3. Three descending cursors, one token-seeded dedup window, and
    an `EndpointState` that never carries a `search_id`."""

    def test_three_descending_pages_accumulate_dedupe_and_publish(
        self, transport: FakeTransport
    ):
        # Record '1' is already in the token's window, and '2' repeats across
        # pages 1 and 2 — both must be dropped exactly once.
        token = _posts_token(cursor=MS + 4000, has_more=True,
                             seen=(fingerprint('1'),))
        _script(transport, [
            posts_reply([1, 2], max_cursor=MS + 3000, has_more=True),
            posts_reply([2, 3], max_cursor=MS + 2000, has_more=True),
            posts_reply([4, 5], max_cursor=MS + 1000, has_more=True),
        ])
        page = _client().user_posts(user_id=UID, sec_uid=SEC, limit=4,
                                   page_token=token)
        assert [c['params']['max_cursor'] for c in transport.calls] == [
            str(MS + 4000), str(MS + 3000), str(MS + 2000)]
        assert [r['id'] for r in page.records] == ['2', '3', '4', '5']
        assert page.cursor == MS + 4000
        assert page.has_more is True, "taken from the LAST page's has_more"
        assert page.next_cursor == MS + 1000
        state, = page.endpoints
        assert state.path == USER_POSTS_PATH
        assert state.cursor == MS + 1000
        assert state.has_more is True
        assert state.started is True
        # A posts stream has no search session. `log_pb.impr_id` is present in
        # every scripted reply above, so an accidental read would show up here.
        assert state.search_id == ''

    def test_an_exhausted_chain_publishes_a_non_resumable_state(
        self, transport: FakeTransport
    ):
        _script(transport, [
            posts_reply([1], max_cursor=MS - 1000, has_more=True),
            posts_reply([2], max_cursor=0, has_more=False),
        ])
        page = _client().user_posts(user_id=UID, sec_uid=SEC, limit=50,
                                   page_token=_posts_token(cursor=MS, has_more=True))
        assert page.has_more is False
        assert page.next_cursor is None
        state, = page.endpoints
        assert state.has_more is False
        assert state.search_id == ''
        # `max_cursor: 0` on the last page must not rewind the stored cursor.
        assert state.cursor == MS - 1000


class TestExhaustedSeedSignsNothing:
    """Property 4. `has_more=False` in the incoming token short-circuits before
    any signing: re-querying the tail would spend a signed request (a PAID one
    under `signer: rapid`) to be told the same thing."""

    def test_no_signed_request_is_made(self, transport: FakeTransport,
                                       rapid_ledger: list):
        transport.script(lambda call: pytest.fail(
            f'an exhausted posts stream signed a request: {call["path"]}'))
        page = _client(rapidapi_key=STUB_SIGNER_KEY).user_posts(
            user_id=UID, sec_uid=SEC, limit=50,
            page_token=_posts_token(cursor=MS, has_more=False))
        assert transport.calls == []
        assert rapid_ledger == []
        assert page.records == []
        assert page.has_more is False
        assert page.next_cursor is None
        assert page.cursor == MS
        state, = page.endpoints
        assert (state.cursor, state.has_more, state.search_id) == (MS, False, '')


# ------------------------------------------------------------- resolve_user
def _user_list(entries: list[dict]) -> dict:
    """A user-search reply built from conftest's canned `users()` shape, with
    the fields this seam reads overridden per entry."""
    base = users(range(1, len(entries) + 1))
    for slot, over in zip(base['user_list'], entries):
        slot['user_info'].update(over)
    return {'status_code': 0, 'has_more': False, **base}


class TestResolveUser:
    """Property 5. Four branches, and the signed-request ledger for each."""

    def test_an_exact_hit_among_fuzzy_neighbours_costs_one_request(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        # User search is FUZZY: taking the first or best-scoring hit would
        # serve a different account's profile. Only an exact, case-insensitive
        # `unique_id` counts.
        transport.script(lambda call: _user_list([
            {'unique_id': 'bakuesaz_fan', 'uid': '1', 'sec_uid': 'SEC-1'},
            {'unique_id': 'BAKUESAZ', 'uid': UID, 'sec_uid': SEC},
            {'unique_id': 'bakuesaz2', 'uid': '3', 'sec_uid': 'SEC-3'},
        ]))
        client = _client(rapidapi_key=STUB_SIGNER_KEY)
        assert client.resolve_user('  BakuEsaz  ') == (UID, SEC)
        assert len(transport.calls) == 1
        assert transport.calls[0]['path'] == SEARCH_USER_PATH
        assert rapid_ledger == []

    def test_the_resolve_sends_the_shared_user_search_params(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: _user_list(
            [{'unique_id': WANTED, 'uid': UID, 'sec_uid': SEC}]))
        _client().resolve_user(WANTED)
        sent = transport.calls[0]['params']
        expected = _user_search_params(WANTED, 0, RESOLVE_USER_COUNT)
        assert {k: sent[k] for k in expected} == expected

    def test_near_misses_only_is_notfound(self, transport: FakeTransport,
                                          rapid_ledger: list):
        transport.script(lambda call: _user_list([
            {'unique_id': 'notzd9', 'uid': '1', 'sec_uid': 'SEC-1'},
            {'unique_id': 'notarealuser9', 'uid': '2', 'sec_uid': 'SEC-2'},
        ]))
        with pytest.raises(NotFound):
            _client(rapidapi_key=STUB_SIGNER_KEY).resolve_user(WANTED)
        assert len(transport.calls) == 1, 'a populated page is not retried'
        assert rapid_ledger == []

    @pytest.mark.parametrize('over,label', [
        ({'unique_id': WANTED, 'uid': UID, 'sec_uid': None}, 'no sec_uid'),
        ({'unique_id': WANTED, 'uid': None, 'sec_uid': SEC}, 'no user_id'),
    ])
    def test_an_exact_hit_with_unusable_ids_is_a_transporterror(
        self, transport: FakeTransport, rapid_ledger: list, over, label
    ):
        # `unique_id` is unique upstream, so this IS the account: not NotFound
        # (TikTok never said the user is gone) and not SoftError (the reply
        # arrived and parsed, so no identity is charged for it).
        transport.script(lambda call: _user_list([dict(over)]))
        with pytest.raises(TransportError) as excinfo:
            _client(rapidapi_key=STUB_SIGNER_KEY).resolve_user(WANTED)
        assert not isinstance(excinfo.value, SoftError), label
        assert not isinstance(excinfo.value, NotFound), label
        assert len(transport.calls) == 1
        assert rapid_ledger == []

    def test_an_entirely_empty_user_list_stays_a_retried_softerror(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        # PINNED. An empty item list on a sessionless first page IS the
        # hit_shark signature, and nothing in the payload separates it from a
        # username that does not exist. Routing it to 404 would launder a
        # possible shadow-block into a definitive claim about the account AND
        # throw away the identity-health report that is the only way the pool
        # learns its warm identity has gone cold. The 502 is deliberate.
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'user_list': []})
        with pytest.raises(SoftError) as excinfo:
            _client(rapidapi_key=STUB_SIGNER_KEY, retries=2).resolve_user(WANTED)
        assert not isinstance(excinfo.value, NotFound)
        # The retry budget IS spent here, unlike every other branch: a
        # shadow-block is retried (which rotates the timestamps), not returned.
        assert len(transport.calls) == 3
        assert rapid_ledger == []

    def test_an_empty_user_list_with_no_retries_costs_one_request(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'user_list': []})
        with pytest.raises(SoftError):
            _client(retries=0).resolve_user(WANTED)
        assert len(transport.calls) == 1

    @pytest.mark.parametrize('unique_id', [999, ['not', 'a', 'string'],
                                           {'nested': 'dict'}, 1.5])
    def test_a_non_string_unique_id_falls_through_to_notfound(
        self, transport: FakeTransport, unique_id
    ):
        # `str(record.get('username') or '').lower()` — a non-string must not
        # reach `.lower()` on the raw value and raise AttributeError, which
        # would escape the API layer as a 500.
        transport.script(lambda call: _user_list(
            [{'unique_id': unique_id, 'uid': UID, 'sec_uid': SEC}]))
        with pytest.raises(NotFound):
            _client().resolve_user(WANTED)
        assert len(transport.calls) == 1

    def test_a_non_dict_user_list_entry_is_skipped(self, transport: FakeTransport):
        transport.script(lambda call: {
            'status_code': 0, 'has_more': False,
            'user_list': ['not-a-dict', None, 7,
                          {'user_info': {'unique_id': WANTED, 'uid': UID,
                                         'sec_uid': SEC}}]})
        assert _client().resolve_user(WANTED) == (UID, SEC)


class TestProfileIdentityHealthSafety:
    """Property 6. The exception CLASS is the property: `pool.run` turns every
    escaping `SoftError` into `report_empty`, so charging a well-formed-but-odd
    payload to the warm identity would retire the only usable device after
    `DEFAULT_STALE_AFTER` of them and 503 every caller."""

    def test_a_uid_less_user_raises_transporterror_not_softerror(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        # Non-empty, so `PROFILE_PAYLOAD.has_payload` passes it — but it
        # flattens to None, and a 200 with a null profile is not an option.
        transport.script(lambda call: {'status_code': 0,
                                       'user': {'unique_id': 'u', 'nickname': 'n'}})
        client = _client(rapidapi_key=STUB_SIGNER_KEY)
        with pytest.raises(TransportError) as excinfo:
            client.profile(UID, SEC)
        assert type(excinfo.value) is TransportError
        assert not isinstance(excinfo.value, SoftError), (
            'a SoftError here is reported to identity health by pool.run')
        assert not isinstance(excinfo.value, NotFound)
        assert len(transport.calls) == 1, 'the payload arrived; nothing is retried'
        assert rapid_ledger == []

    def test_a_populated_user_returns_a_record_in_one_request(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        transport.script(lambda call: {'status_code': 0, 'user': {
            'uid': UID, 'sec_uid': SEC, 'unique_id': WANTED,
            'nickname': 'Baku Esaz', 'follower_count': '12',
            'total_favorited': 34, 'secret': '0'}})
        record = _client(rapidapi_key=STUB_SIGNER_KEY).profile(UID, SEC)
        assert record['user_id'] == UID
        assert record['sec_uid'] == SEC
        assert record['username'] == WANTED
        assert len(transport.calls) == 1
        assert transport.calls[0]['path'] == USER_PROFILE_PATH
        assert rapid_ledger == []

    def test_the_profile_request_sends_both_ids_and_no_address_book(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: {'status_code': 0,
                                       'user': {'uid': UID, 'sec_uid': SEC}})
        _client().profile(UID, SEC)
        sent = transport.calls[0]['params']
        assert sent['user_id'] == UID
        assert sent['sec_user_id'] == SEC
        assert sent['address_book_access'] == '0'


class TestPaidSignerLedgerIsZero:
    """Property 7. Swapping signers on an empty is what
    `.claude/rules/lessons/anti-block.md` forbids, so the assertion is the
    receipt (0 constructions) and not merely which exception came out. The
    client is given a key, so a fallback COULD be built if the wiring let it."""

    def test_a_posts_chain_builds_no_paid_signer(self, transport: FakeTransport,
                                                 rapid_ledger: list):
        _script(transport, [posts_reply([1], max_cursor=MS, has_more=True),
                            posts_reply([2], max_cursor=MS - 1, has_more=False)])
        client = _client(rapidapi_key=STUB_SIGNER_KEY)
        _paginate(client, limit=50, start_cursor=POSTS_START_CURSOR)
        assert rapid_ledger == []
        assert client._fallback is None

    def test_a_posts_reply_that_dropped_its_payload_builds_no_paid_signer(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        # An ABSENT `aweme_list` is risk-control, which is exactly the empty
        # the fallback must refuse to answer.
        transport.script(lambda call: posts_reply([], max_cursor=MS,
                                                  has_more=True, omit_list=True))
        client = _client(rapidapi_key=STUB_SIGNER_KEY, retries=2)
        with pytest.raises(SoftError):
            _paginate(client, limit=50, start_cursor=POSTS_START_CURSOR)
        assert len(transport.calls) == 3
        assert rapid_ledger == []
        assert client._fallback is None


class TestUserSearchParamsPin:
    """Property 8. The extraction of `_user_search_params` must be a pure
    refactor: `/search`'s user path sends the same keys, in the same order,
    with the same values it sent before, or the SIGNED URL has drifted."""

    # Verbatim from the pre-extraction literal in `_search_users`, at the
    # direct-mode page size (count=10) and the first cursor.
    LEGACY = {'keyword': 'ocean', 'count': '10', 'cursor': '0', 'type': '1',
              'search_source': 'normal_search'}

    def test_the_helper_reproduces_the_legacy_dict_exactly(self):
        built = _user_search_params('ocean', 0, 10)
        assert built == self.LEGACY
        assert list(built) == list(self.LEGACY), 'key ORDER feeds urlencode'

    def test_the_numeric_params_are_stringified(self):
        built = _user_search_params('ocean', 30, 20)
        assert built['cursor'] == '30'
        assert built['count'] == '20'
        assert all(isinstance(v, str) for v in built.values())

    def test_a_real_user_search_sends_the_legacy_params(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'cursor': 0, **users([1, 2])})
        page = _client().search(SearchQuery(kind=SearchKind.USER, term='ocean',
                                            limit=10))
        assert len(page.records) == 2
        sent = transport.calls[0]['params']
        assert {k: sent.get(k) for k in self.LEGACY} == self.LEGACY
        assert transport.calls[0]['path'] == SEARCH_USER_PATH
