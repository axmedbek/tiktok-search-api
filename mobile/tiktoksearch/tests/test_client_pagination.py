"""Unit tests for client.py pagination: the search SESSION, TikTok's cursor,
and the classification of an empty reply.

Everything here runs the REAL `_get_signed` / `_paginate_into` / merged-endpoint
code against scripted replies: the only stub is `requests.Session.get` (plus the
signer class, from conftest), because the behaviour under test IS the
classification of a reply and the number of PAID signed requests spent on it.
`transport.calls` is that ledger — a regression that adds a retry storm or an
endless chain of empty pages shows up there as a count, which is the form the
money-burning bugs on this branch actually took.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    STUB_SIGNER_KEY,
    FakeResponse,
    empty_reply,
    reply,
    users,
)

from tiktoksearch.client import (  # noqa: E402
    MAX_PAGES_PER_ENDPOINT,
    NIL_EMPTY_SESSION,
    NIL_FEDERATION_EMPTY,
    SEARCH_ITEM_PATH,
    SEARCH_USER_PATH,
    SEARCH_VIDEO_PATH,
    PageProgress,
    TikTokClient,
    _prefer_failure,
)
from tiktoksearch.config import ClientConfig  # noqa: E402
from tiktoksearch.errors import SoftError, TransportError  # noqa: E402
from tiktoksearch.filters import SearchKind, SearchQuery  # noqa: E402
from tiktoksearch.mapping import flatten_video  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    MAX_ENDPOINT_CURSOR,
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    SeenWindow,
    device_handle,
    encode,
    fingerprint,
)

# A nil value this code has never seen. The literal ByteDance sends for
# risk-control is not pinned down anywhere and it changes, so the tail test is
# an ALLOW-list: anything unrecognised must still be risk-control.
NOVEL_NIL = 'nil_shape_nobody_has_seen_yet'
RISK_CONTROL_NIL = 'hit_shark'
RETRIES = 2
ATTEMPTS = RETRIES + 1
SESSION_ID = 'SIDP'
STORED_CURSOR = 30


def _client(**over) -> TikTokClient:
    """A direct-mode client. `RapidSigner` is the conftest fake, so no real
    signer object exists and nothing is ever signed for real."""
    config = ClientConfig(rapidapi_key=STUB_SIGNER_KEY, retries=RETRIES,
                          device_id='DEVA', iid='IIDA',
                          device_query={'device_id': 'DEVA', 'iid': 'IIDA'}, **over)
    return TikTokClient(config)


def _token(*endpoints: EndpointState, seen: tuple[bytes, ...] = ()) -> PageToken:
    return PageToken(version=TOKEN_VERSION, query_hash='q' * 32,
                     device_handle=device_handle('DEVA'),
                     endpoints=endpoints, seen=seen)


def _resumable_primary(cursor: int = STORED_CURSOR) -> EndpointState:
    return EndpointState(SEARCH_VIDEO_PATH, cursor, SESSION_ID, True, True)


def _retired_secondary() -> EndpointState:
    return EndpointState(SEARCH_ITEM_PATH, 0, '', False, True)


def _unopened_secondary() -> EndpointState:
    return EndpointState(SEARCH_ITEM_PATH, 0, '', True, False)


def _query(*, token: PageToken | None = None, limit: int = 20, cursor: int = 0,
           kind: SearchKind = SearchKind.KEYWORD, term: str = 'ocean') -> SearchQuery:
    return SearchQuery(kind=kind, term=term, limit=limit, cursor=cursor, page_token=token)


def _build(offset: int, count: int) -> dict:
    return {'keyword': 'ocean', 'count': str(count), 'offset': str(offset)}


def _unwrap(item: dict) -> dict | None:
    return item.get('aweme_info') or item


def _drive(client: TikTokClient, *, start_cursor: int = 0, limit: int = 20,
           search_id: str = '', seen: SeenWindow | None = None,
           progress: PageProgress | None = None):
    """Run one endpoint's inner loop and hand back (records, PageEnd)."""
    out: list[dict] = []
    end = client._paginate_into(
        out=out, seen=seen or SeenWindow(), path=SEARCH_VIDEO_PATH,
        build_params=_build, items_key='data', unwrap=_unwrap, flatten=flatten_video,
        source_term='search:ocean', limit=limit, start_cursor=start_cursor,
        search_id=search_id, progress=progress)
    return out, end


def _ids(records: list[dict]) -> list[str]:
    return [record['id'] for record in records]


def _state(page, path: str) -> EndpointState:
    return next(state for state in page.endpoints if state.path == path)


class TestContinuationNilMatrix:
    """The five rows of "what does an empty reply to a request that CARRIED a
    search_id mean". Getting this wrong in either direction is expensive: read a
    tail as risk-control and three ordinary "load more" clicks retire the only
    warm identity (503 for everyone); read risk-control as a tail and a
    shadow-block becomes a silent 200 that IdentityStore never sees."""

    def _continuation(self, transport, payload) -> SearchQuery:
        # One resumable endpoint only, so the sign ledger is unambiguous.
        transport.script(lambda call: payload)
        return _query(token=_token(_resumable_primary(), _retired_secondary()))

    @pytest.mark.parametrize('nil', [NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY, None])
    def test_an_allow_listed_tail_is_returned_as_data(self, transport, nil):
        query = self._continuation(transport, empty_reply(nil=nil, has_more=False))
        page = _client().search(query)
        assert page.records == []
        assert page.has_more is False
        assert page.next_cursor is None

    @pytest.mark.parametrize('nil', [NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY, None])
    def test_a_tail_costs_exactly_one_signed_request(self, transport, nil):
        # Re-signing cannot revive a finished session, so the retry budget must
        # not be spent on one.
        query = self._continuation(transport, empty_reply(nil=nil, has_more=False))
        _client().search(query)
        assert len(transport.calls) == 1

    @pytest.mark.parametrize('nil', [NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY, None])
    def test_a_tail_retires_the_endpoint_it_answered(self, transport, nil):
        query = self._continuation(transport, empty_reply(nil=nil, has_more=False))
        primary = _state(_client().search(query), SEARCH_VIDEO_PATH)
        assert primary.has_more is False
        assert primary.started is True

    @pytest.mark.parametrize('nil', [RISK_CONTROL_NIL, NOVEL_NIL])
    def test_risk_control_on_a_continuation_is_a_soft_error(self, transport, nil):
        # A session does NOT launder an unrecognised nil into a tail: the allow
        # list fails closed, or a shadow-block mid-stream returns a silent 200.
        query = self._continuation(transport, empty_reply(nil=nil, has_more=False))
        with pytest.raises(SoftError):
            _client().search(query)
        assert len(transport.calls) == ATTEMPTS

    @pytest.mark.parametrize('nil', [RISK_CONTROL_NIL, NOVEL_NIL])
    def test_risk_control_with_has_more_true_is_still_a_soft_error(self, transport, nil):
        query = self._continuation(transport, empty_reply(nil=nil, has_more=True))
        with pytest.raises(SoftError):
            _client().search(query)

    @pytest.mark.parametrize('nil', [NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY])
    def test_a_tail_is_terminal_even_when_the_reply_says_has_more_true(
            self, transport, nil):
        # The bug this closes: classifying the reply as "nothing more to give"
        # and then reading has_more:true off the SAME reply published a
        # resumable endpoint, minted a token, and every follow-up spent another
        # paid sign on the same empty answer — a stream that never terminates.
        query = self._continuation(transport, empty_reply(nil=nil, has_more=True))
        page = _client().search(query)
        assert page.has_more is False
        assert _state(page, SEARCH_VIDEO_PATH).has_more is False
        assert len(transport.calls) == 1

    def test_a_tail_carrying_records_still_serves_every_record(self, transport):
        transport.script(lambda call: reply(range(1, 6), cursor=40, has_more=True,
                                            nil=NIL_EMPTY_SESSION))
        page = _client().search(
            _query(token=_token(_resumable_primary(), _retired_secondary())))
        assert len(page.records) == 5
        assert _state(page, SEARCH_VIDEO_PATH).has_more is False
        assert len(transport.calls) == 1

    def test_a_tail_does_not_rewind_the_stored_cursor(self, transport):
        # TikTok answers a finished session with `cursor: 0`. Storing that would
        # send the next continuation back over a window this stream already
        # served, at one paid sign per page.
        transport.script(lambda call: empty_reply(nil=NIL_EMPTY_SESSION, cursor=0))
        page = _client().search(
            _query(token=_token(_resumable_primary(), _retired_secondary())))
        assert _state(page, SEARCH_VIDEO_PATH).cursor == STORED_CURSOR


class TestSessionlessEmptyIsRiskControl:
    """Anti-block invariant (a) on the path where it actually guards: a first
    page carries no session, so an empty answer is a shadow-block."""

    def test_a_sessionless_empty_page_one_raises_soft_error(self, transport):
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        with pytest.raises(SoftError):
            _client().search(_query())

    def test_a_sessionless_page_one_carries_no_search_id(self, transport):
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        with pytest.raises(SoftError):
            _client().search(_query())
        assert transport.sids == [None] * len(transport.calls)

    @pytest.mark.parametrize('nil', [NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY, None])
    def test_the_tail_branch_never_applies_without_a_session(self, transport, nil):
        # The same reply that is a legitimate tail on a continuation is
        # risk-control on a first page. `has_session` is the whole difference.
        transport.script(lambda call: empty_reply(nil=nil, has_more=False))
        with pytest.raises(SoftError):
            _client().search(_query())


class TestTikTokCursorIsAuthoritative:
    def test_the_reply_cursor_wins_over_start_plus_records(self, transport):
        # Dedup drops items, so `start_cursor + len(out)` diverges from TikTok's
        # own cursor — and the next request must resume at TikTok's.
        transport.script(lambda call: reply(range(1, 11), cursor=25, has_more=True))
        out, end = _drive(_client(), limit=5)
        assert len(out) == 5
        assert end.cursor == 25

    def test_a_numeric_string_cursor_is_coerced(self, transport):
        transport.script(lambda call: reply(range(1, 11), cursor='25', has_more=True))
        _, end = _drive(_client(), limit=5)
        assert end.cursor == 25

    def test_an_absent_cursor_falls_back_to_the_computed_offset(self, transport):
        transport.script(lambda call: reply(range(1, 11), cursor=None, has_more=True))
        _, end = _drive(_client(), limit=10)
        assert end.cursor == 10

    def test_an_unusable_cursor_falls_back_to_the_computed_offset(self, transport):
        transport.script(lambda call: reply(range(1, 11), cursor='not-a-number',
                                            has_more=True))
        _, end = _drive(_client(), limit=10)
        assert end.cursor == 10

    def test_a_cursor_of_zero_cannot_rewind_a_stored_cursor(self, transport):
        transport.script(lambda call: empty_reply(nil=NIL_EMPTY_SESSION, cursor=0))
        _, end = _drive(_client(), start_cursor=STORED_CURSOR, search_id=SESSION_ID)
        assert end.cursor == STORED_CURSOR
        assert end.has_more is False

    def test_a_cursor_beyond_the_resumable_bound_retires_the_endpoint(self, transport):
        # Minting a cursor past decode()'s bound would produce a token whose
        # very next request is 422'd, so the endpoint is clamped and retired.
        transport.script(lambda call: reply(range(1, 11),
                                            cursor=MAX_ENDPOINT_CURSOR + 50,
                                            has_more=True))
        _, end = _drive(_client(), start_cursor=MAX_ENDPOINT_CURSOR, limit=100)
        assert end.cursor == MAX_ENDPOINT_CURSOR
        assert end.has_more is False

    def test_the_clamped_out_of_bounds_state_still_encodes(self, transport):
        transport.script(lambda call: reply(range(1, 11),
                                            cursor=MAX_ENDPOINT_CURSOR + 50,
                                            has_more=True))
        _, end = _drive(_client(), start_cursor=MAX_ENDPOINT_CURSOR, limit=100)
        raw = encode(_token(EndpointState(SEARCH_VIDEO_PATH, end.cursor,
                                          end.search_id, end.has_more, True)))
        assert raw  # a token the very next request would 422 must not be mintable


class TestLoopTermination:
    """Every iteration of the inner loop is a PAID signed request."""

    def test_a_non_advancing_cursor_stops_on_the_second_page(self, transport):
        pages = {'0': reply(range(1, 11), cursor=10, has_more=True),
                 '10': reply(range(11, 21), cursor=10, has_more=True)}
        transport.script(lambda call: pages[call['offset']])
        out, end = _drive(_client(), limit=100)
        # Caught by the guard (which compares against the PREVIOUS iteration),
        # not by the page ceiling — the ceiling would have cost 12 signs.
        assert len(transport.calls) == 2
        assert len(transport.calls) < MAX_PAGES_PER_ENDPOINT
        assert end.has_more is False
        assert len(out) == 20

    def test_a_rewinding_cursor_is_caught_by_the_same_guard(self, transport):
        pages = {'0': reply(range(1, 11), cursor=10, has_more=True),
                 '10': reply(range(11, 21), cursor=5, has_more=True)}
        transport.script(lambda call: pages[call['offset']])
        _, end = _drive(_client(), limit=100)
        assert len(transport.calls) == 2
        assert end.cursor == 10        # clamped, never rewound to 5
        assert end.has_more is False

    def test_the_page_ceiling_backstops_a_stream_that_only_repeats_itself(
            self, transport):
        # Cursor advances every page, so the guard cannot fire; every page
        # re-serves ids the dedup window already holds.
        transport.script(lambda call: reply(range(1, 11),
                                            cursor=int(call['offset']) + 10,
                                            has_more=True))
        out, end = _drive(_client(), limit=100)
        assert len(transport.calls) == MAX_PAGES_PER_ENDPOINT
        assert end.has_more is False
        assert len(out) == 10

    def test_a_normally_advancing_stream_walks_the_offsets(self, transport):
        pages = {'0': reply(range(1, 11), cursor=10, has_more=True),
                 '10': reply(range(11, 21), cursor=20, has_more=False)}
        transport.script(lambda call: pages[call['offset']])
        out, end = _drive(_client(), limit=100)
        assert transport.offsets == ['0', '10']
        assert len(out) == 20
        assert end.cursor == 20
        assert end.has_more is False


class TestSessionThreading:
    def test_the_first_page_carries_no_session_and_the_second_echoes_it(
            self, transport):
        pages = {'0': reply(range(1, 11), cursor=10, has_more=True, sid='SID1'),
                 '10': reply(range(11, 21), cursor=20, has_more=False, sid='SID2')}
        transport.script(lambda call: pages[call['offset']])
        _drive(_client(), limit=100)
        assert transport.sids == [None, 'SID1']

    def test_a_continuation_signs_its_first_request_with_the_token_state(
            self, transport):
        transport.script(lambda call: empty_reply(nil=NIL_EMPTY_SESSION))
        _client().search(_query(token=_token(_resumable_primary(), _retired_secondary())))
        assert transport.offsets == [str(STORED_CURSOR)]
        assert transport.sids == [SESSION_ID]

    def test_an_unusable_impr_id_keeps_the_session_already_in_hand(self, transport):
        # sanitize_search_id returns '' for an id decode would reject; the
        # caller must fall back to the session it had, not drop it.
        pages = {'0': reply(range(1, 11), cursor=10, has_more=True, sid='SID1'),
                 '10': reply(range(11, 21), cursor=20, has_more=True,
                             sid='BAD SID!'),
                 '20': reply(range(21, 31), cursor=30, has_more=False, sid='SID3')}
        transport.script(lambda call: pages[call['offset']])
        _drive(_client(), limit=100)
        assert transport.sids == [None, 'SID1', 'SID1']


class TestEndpointRetirementOnFailure:
    def test_a_failure_after_records_leaves_the_endpoint_resumable(self, transport):
        # PageProgress: the endpoint served records and holds a live cursor +
        # session, so a 25s-timeout hiccup on inner page 2 must not amputate it
        # for the rest of the stream.
        def handler(call):
            if call['offset'] == str(STORED_CURSOR):
                return reply(range(1, 11), cursor=40, has_more=True, sid='SID2')
            return empty_reply(nil=RISK_CONTROL_NIL)

        transport.script(handler)
        page = _client().search(
            _query(token=_token(_resumable_primary(), _retired_secondary()), limit=20))
        primary = _state(page, SEARCH_VIDEO_PATH)
        assert len(page.records) == 10
        assert primary.has_more is True
        assert primary.cursor == 40
        assert primary.search_id == 'SID2'
        assert primary.started is True

    def test_a_soft_error_with_nothing_obtained_retires_the_endpoint(self, transport):
        # An empty answer is TikTok's has_more=false wearing the sessionless
        # heuristic's clothes; keeping it alive re-pays retries+1 signs a page.
        def handler(call):
            if call['path'] == SEARCH_VIDEO_PATH:
                return empty_reply(nil=RISK_CONTROL_NIL)
            return reply(range(1, 11), cursor=10, has_more=False,
                         key='search_item_list')

        transport.script(handler)
        page = _client().search(
            _query(token=_token(_resumable_primary(), _unopened_secondary()), limit=20))
        primary = _state(page, SEARCH_VIDEO_PATH)
        assert len(page.records) == 10      # the secondary's records still ship
        assert primary.has_more is False
        assert primary.cursor == STORED_CURSOR

    def test_a_transport_error_with_nothing_obtained_stays_resumable(self, transport):
        # Evidence about the network, not about the endpoint.
        def handler(call):
            if call['path'] == SEARCH_VIDEO_PATH:
                return FakeResponse({}, status=500)
            return reply(range(1, 11), cursor=10, has_more=False,
                         key='search_item_list')

        transport.script(handler)
        page = _client().search(
            _query(token=_token(_resumable_primary(), _unopened_secondary()), limit=20))
        primary = _state(page, SEARCH_VIDEO_PATH)
        assert len(page.records) == 10
        assert primary.has_more is True
        assert primary.cursor == STORED_CURSOR

    def test_an_empty_page_with_a_failure_present_still_raises(self, transport):
        # Invariant (a) survives the move to a post-loop raise: records-or-raise.
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        with pytest.raises(SoftError):
            _client().search(_query())

    def test_a_soft_error_outranks_a_transport_error_in_either_order(self):
        soft, transport_exc = SoftError('empty'), TransportError('timeout')
        assert _prefer_failure(transport_exc, soft) is soft
        assert _prefer_failure(soft, transport_exc) is soft
        assert _prefer_failure(None, transport_exc) is transport_exc
        assert _prefer_failure(soft, None) is soft


class TestCrossRequestDedup:
    """Opening the second endpoint MID-STREAM must not re-serve what the first
    page already gave the caller — demo.html concatenates pages."""

    # Ids an earlier page of this stream already handed the caller.
    SERVED = tuple(str(i) for i in range(1, 21))
    LIMIT = 15

    def _handler(self, call):
        if call['path'] == SEARCH_VIDEO_PATH:
            # The primary resumes at its stored cursor and finishes.
            return reply(range(21, 31), cursor=40, has_more=False)
        # The secondary is opened for the FIRST time here, at cursor 0: its
        # window necessarily overlaps what the primary already served, then
        # continues into ids the stream has never seen.
        return reply(list(range(1, 11)) + list(range(31, 36)), cursor=15,
                     has_more=True, key='search_item_list')

    def _run(self, transport, *, seed: bool):
        transport.script(self._handler)
        seen = tuple(fingerprint(key) for key in self.SERVED) if seed else ()
        token = _token(_resumable_primary(), _unopened_secondary(), seen=seen)
        return _client().search(_query(token=token, limit=self.LIMIT))

    def test_a_seeded_window_produces_zero_duplicates(self, transport):
        page = self._run(transport, seed=True)
        assert set(_ids(page.records)).isdisjoint(self.SERVED)
        assert _ids(page.records) == [str(i) for i in range(21, 36)]

    def test_the_empty_window_control_does_duplicate(self, transport):
        # The control is what proves the dedup STATE — not the fixture — is
        # responsible for the zero overlap above. Same endpoint states, same
        # scripted replies, only `seen` differs.
        page = self._run(transport, seed=False)
        served_again = set(_ids(page.records)) & set(self.SERVED)
        assert served_again == {'1', '2', '3', '4', '5'}
        # And the duplicates ate the page budget, so the genuinely new ids the
        # seeded run reached were never served at all.
        assert '31' not in _ids(page.records)

    def test_dedup_does_not_empty_the_page(self, transport):
        page = self._run(transport, seed=True)
        assert len(page.records) == self.LIMIT
        assert page.has_more is True
        assert _state(page, SEARCH_ITEM_PATH).has_more is True


class TestUserSearchItemKey:
    def test_a_user_page_answering_has_more_false_is_not_a_shadow_block(
            self, transport):
        # `user_list` belongs in _ITEM_LIST_KEYS: without it this reply reads as
        # "no items, has_more=false" and 502s while carrying users.
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'cursor': 0, 'log_pb': {'impr_id': 'SIDU'},
                                       **users(range(1, 4))})
        page = _client().search(_query(kind=SearchKind.USER, term='nasa', limit=10))
        assert len(page.records) == 3
        assert page.records[0]['username'] == 'u1'
        assert page.has_more is False
        assert transport.paths == [SEARCH_USER_PATH]

    def test_a_genuinely_empty_user_page_is_still_risk_control(self, transport):
        transport.script(lambda call: {'status_code': 0, 'has_more': False,
                                       'cursor': 0, 'user_list': []})
        with pytest.raises(SoftError):
            _client().search(_query(kind=SearchKind.USER, term='nasa', limit=10))


class TestSearchQueryTokenExclusivity:
    def test_a_token_together_with_a_non_zero_cursor_is_rejected(self):
        with pytest.raises(ValueError):
            _query(token=_token(_resumable_primary()), cursor=10)

    def test_a_token_with_cursor_zero_is_accepted(self):
        assert _query(token=_token(_resumable_primary())).page_token is not None
