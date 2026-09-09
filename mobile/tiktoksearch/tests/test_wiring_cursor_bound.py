"""Stubbed contract checks for the PER-PATH cursor bound (Epic subtask 4).

Subtask 4 turned `paging`'s single `MAX_ENDPOINT_CURSOR` into a per-path
selector (`cursor_bound`) so that a posts `max_cursor` — a millisecond epoch,
~1.79e12 — can ride in a page_token while a search offset above 100_000 stays
rejected. It shipped with no tests, and the gap was demonstrable: deleting
`_MS_EPOCH_CURSOR_PATHS` and collapsing `cursor_bound` to a single `return`
left the whole suite green. This file closes that.

The properties are CONTRACT properties, checked across the module boundary
`paging` cannot cross on its own:

1. Both bounds are enforced on BOTH sides of the token. A posts cursor AT the
   ms-epoch bound round-trips `encode`->`decode`; one above it is `MALFORMED`;
   `encode` refuses to mint either an over-bound or a negative cursor with
   `_UNMINTABLE_CURSOR`. A server that mints a token it then 422s is worse than
   one that refuses to mint.
2. The selection FAILS CLOSED. An unknown path — including one this module has
   never heard of — gets the strict offset bound, and each of the three search
   paths still gets exactly `MAX_ENDPOINT_CURSOR`, so the `_paginate_into`
   refactor from the imported constant to `cursor_bound(path)` is provably the
   identity on `/search`.
3. The two SPELLINGS of the posts path agree. `paging` cannot import
   `client` (the import would close a cycle), so it repeats the posts path as
   its own literal. A test may import both, so
   `cursor_bound(client.USER_POSTS_PATH) == MAX_MS_EPOCH_CURSOR` is what makes
   that duplication safe over time, at zero layering cost. If the spellings
   ever diverge, this is the only thing that will say so.
4. `MAX_PAGE_TOKEN_CHARS` really bounds every mintable shape. The two existing
   size tests cannot catch a wrong `_widest_endpoint()`: one asserts
   `MAX_PAGE_TOKEN_CHARS == len(_wire(_worst_case()))`, which restates the
   definition, and the other compares the worst case to itself — both stay
   green if the derivation under-estimates every real shape. So the widest
   mintable token is built INDEPENDENTLY here, once per real endpoint path plus
   a maximum-length unknown one, and the literal `3134` is pinned because it is
   also `SearchRequest.page_token`'s Pydantic `max_length`.
5. The bound closed a 500. `_paginate_posts` folded it into its progress test,
   so an upstream `max_cursor` past the calendar ceiling now RETIRES the
   endpoint instead of being published as resume state, reaching `encode` and
   raising `ValueError` out of `run_in_executor` on a request that had already
   served records. Driven at both seams: out of bounds on page 1, and on page 2
   after a legitimate walk-back.

Nothing leaves the process. `paging`'s functions are pure; the
`_paginate_posts` checks answer from conftest's `transport` fixture over
`requests.Session.get`, so the session-wide `HTTPAdapter.send` tripwire still
holds. The module-private `_tag` / `_b64encode` are used the same way
`test_paging.py` uses them: forging an AUTHENTICALLY signed token is the only
way to reach the cursor check that sits behind the HMAC gate, since `encode`
refuses to mint an out-of-bound cursor by design.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import FakeTransport, local_config, posts_reply  # noqa: E402

from tiktoksearch import paging  # noqa: E402
from tiktoksearch.client import (  # noqa: E402
    ENDPOINT_PATHS,
    POSTS_START_CURSOR,
    SEARCH_PATHS,
    SEARCH_VIDEO_PATH,
    USER_POSTS_PATH,
    TikTokClient,
)
from tiktoksearch.paging import (  # noqa: E402
    MAX_ENDPOINT_CURSOR,
    MAX_ENDPOINT_PATH_CHARS,
    MAX_ENDPOINTS,
    MAX_MS_EPOCH_CURSOR,
    MAX_PAGE_TOKEN_CHARS,
    MAX_SEARCH_ID_CHARS,
    MAX_SEEN_FINGERPRINTS,
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    cursor_bound,
    decode,
    device_handle,
    encode,
    query_hash,
)

LOGGER = 'tiktoksearch.client'
MALFORMED = paging._MALFORMED
UNMINTABLE_CURSOR = paging._UNMINTABLE_CURSOR

QUERY_HASH = query_hash('keyword', 'ocean', {})
DEVICE = device_handle('DEVA')

# A posts token's allow-list. Spelled locally, not imported: `api/app.py` has
# no posts handler yet (subtask 6), and subtask 3's carry-forward is that posts
# must decode against a posts-ONLY allow-list, never `ENDPOINT_PATHS` wholesale.
POSTS_PATHS = frozenset((USER_POSTS_PATH,))
# A path no module knows. It is deliberately NOT in any allow-list: the point
# is that `cursor_bound` fails closed on it anyway.
UNKNOWN_PATH = '/aweme/v1/anything/'
# The longest path `decode` will accept. Bounds what the derived size cap has
# to cover; unreachable by any real producer, whose paths are module constants.
LONGEST_PATH = 'p' * MAX_ENDPOINT_PATH_CHARS

# Millisecond-epoch cursors, descending — the shape a posts reply answers with.
MS = 1_700_000_000_000
UID = '9'
SEC = 'SEC-9'


def _token(*endpoints: EndpointState, seen: tuple[bytes, ...] = ()) -> PageToken:
    return PageToken(version=TOKEN_VERSION, query_hash=QUERY_HASH,
                     device_handle=DEVICE, endpoints=endpoints, seen=seen)


def _decode(raw: str, *, paths=POSTS_PATHS) -> PageToken:
    return decode(raw, expected_query_hash=QUERY_HASH, allowed_paths=paths)


def _sign(payload: object) -> str:
    """`body.tag` with an AUTHENTIC tag, so the forged payload behind it is
    actually reached. `encode` will not mint an out-of-bound cursor, so this is
    the only way to put one in front of `decode`."""
    body = paging._b64encode(json.dumps(payload).encode('utf-8'))
    return body + '.' + paging._b64encode(paging._tag(body))


def _forged(*, path: str, cursor: int) -> str:
    return _sign({'v': TOKEN_VERSION, 'q': QUERY_HASH, 'dev': DEVICE, 'seen': '',
                  'eps': [{'p': path, 'c': cursor, 'sid': 'SID1',
                           'm': True, 's': True}]})


def _reason(raw: str, **kw) -> str:
    with pytest.raises(ValueError) as excinfo:
        _decode(raw, **kw)
    return str(excinfo.value)


def _widest_mintable(path: str) -> PageToken:
    """The largest token `encode` can mint whose endpoints all name `path`.

    Derived here from the field bounds directly, NOT from `paging._worst_case`
    — reusing that helper is what makes the two existing size tests
    tautological. Every field is at its own maximum and the cursor at THIS
    path's own bound, which is the only shape the per-path selector permits.
    `has_more`/`started` are False because 'false' is a character longer than
    'true'."""
    entry = EndpointState(path=path, cursor=cursor_bound(path),
                          search_id='s' * MAX_SEARCH_ID_CHARS,
                          has_more=False, started=False)
    return PageToken(
        version=TOKEN_VERSION, query_hash='f' * 32, device_handle='f' * 16,
        endpoints=(entry,) * MAX_ENDPOINTS,
        seen=(b'\xff' * paging.FINGERPRINT_BYTES,) * MAX_SEEN_FINGERPRINTS)


class TestPostsCursorRoundTrip:
    """Check 1. The ms-epoch bound is enforced on BOTH sides of the token."""

    def test_a_posts_cursor_at_the_ms_epoch_bound_round_trips(self):
        raw = encode(_token(EndpointState(USER_POSTS_PATH, MAX_MS_EPOCH_CURSOR, '')))
        assert _decode(raw).endpoints[0].cursor == MAX_MS_EPOCH_CURSOR

    def test_a_realistic_posts_cursor_round_trips(self):
        # The value a live reply actually answers with, ~1.7e12 — 7 orders of
        # magnitude past MAX_ENDPOINT_CURSOR, i.e. every posts continuation
        # would have 422'd before this subtask.
        raw = encode(_token(EndpointState(USER_POSTS_PATH, MS, '')))
        assert _decode(raw).endpoints[0].cursor == MS

    def test_one_past_the_ms_epoch_bound_is_malformed(self):
        assert _reason(_forged(path=USER_POSTS_PATH,
                               cursor=MAX_MS_EPOCH_CURSOR + 1)) == MALFORMED


class TestSearchCursorBoundIsUnchanged:
    """Check 2. The laxer bound must not have leaked onto the search family.

    `test_paging.py::test_out_of_bounds_cursor_is_malformed` covers the same
    ground through `_endpoint()`'s DEFAULT path; this states the path
    explicitly so it cannot silently start exercising the posts bound if that
    default is ever edited."""

    def test_a_search_cursor_one_past_the_offset_bound_is_malformed(self):
        raw = _forged(path=SEARCH_VIDEO_PATH, cursor=MAX_ENDPOINT_CURSOR + 1)
        assert _reason(raw, paths=SEARCH_PATHS) == MALFORMED

    def test_a_search_cursor_at_a_posts_legal_value_is_still_malformed(self):
        # The exact value check 1 accepts on the posts path.
        raw = _forged(path=SEARCH_VIDEO_PATH, cursor=MAX_MS_EPOCH_CURSOR)
        assert _reason(raw, paths=SEARCH_PATHS) == MALFORMED

    def test_the_search_offset_bound_itself_is_still_accepted(self):
        raw = _forged(path=SEARCH_VIDEO_PATH, cursor=MAX_ENDPOINT_CURSOR)
        token = _decode(raw, paths=SEARCH_PATHS)
        assert token.endpoints[0].cursor == MAX_ENDPOINT_CURSOR


class TestEncodeRefusesAnUnmintableCursor:
    """Check 3. Nothing here is droppable — the cursor IS the resumable state
    — so `encode` raises rather than trimming. The reason string is asserted,
    matching the file's convention, so this is distinguishable from `_TOO_LONG`
    (which the dedup window can still be shed to avoid)."""

    @pytest.mark.parametrize('path,cursor,label', [
        (SEARCH_VIDEO_PATH, MAX_ENDPOINT_CURSOR + 1, 'search, one past its offset bound'),
        (SEARCH_VIDEO_PATH, MAX_MS_EPOCH_CURSOR, 'search, at the ms-epoch bound'),
        (USER_POSTS_PATH, MAX_MS_EPOCH_CURSOR + 1, 'posts, one past its ms-epoch bound'),
        (UNKNOWN_PATH, MAX_ENDPOINT_CURSOR + 1, 'unknown path, one past the strict bound'),
    ])
    def test_encode_refuses_a_cursor_over_its_own_path_s_bound(self, path, cursor, label):
        with pytest.raises(ValueError) as excinfo:
            encode(_token(EndpointState(path, cursor, '')))
        assert str(excinfo.value) == UNMINTABLE_CURSOR, label

    @pytest.mark.parametrize('path', [SEARCH_VIDEO_PATH, USER_POSTS_PATH])
    def test_encode_refuses_a_negative_cursor_on_either_family(self, path):
        with pytest.raises(ValueError) as excinfo:
            encode(_token(EndpointState(path, -1, '')))
        assert str(excinfo.value) == UNMINTABLE_CURSOR

    def test_a_second_endpoint_over_its_bound_is_caught_too(self):
        # The check loops over EVERY endpoint, not just the first: a merged
        # search page carries up to MAX_ENDPOINTS of them.
        with pytest.raises(ValueError) as excinfo:
            encode(_token(EndpointState(SEARCH_VIDEO_PATH, 10, 'SID1'),
                          EndpointState(SEARCH_VIDEO_PATH, MAX_ENDPOINT_CURSOR + 1, 'SID2')))
        assert str(excinfo.value) == UNMINTABLE_CURSOR

    @pytest.mark.parametrize('path', sorted(ENDPOINT_PATHS))
    def test_encode_accepts_every_real_path_at_its_own_bound(self, path):
        # The complement: the check must not be so strict that a legitimate
        # resume state cannot be minted.
        assert encode(_token(EndpointState(path, cursor_bound(path), 'SID1')))


class TestCursorBoundSelection:
    """Checks 4 and 5. The selector fails closed, and the two spellings of the
    posts path agree."""

    def test_an_unknown_path_gets_the_strict_offset_bound(self):
        assert cursor_bound(UNKNOWN_PATH) == MAX_ENDPOINT_CURSOR

    @pytest.mark.parametrize('path', ['', '/', LONGEST_PATH,
                                      USER_POSTS_PATH.rstrip('/'),
                                      USER_POSTS_PATH.upper(),
                                      ' ' + USER_POSTS_PATH])
    def test_a_near_miss_of_the_posts_path_gets_the_strict_bound(self, path):
        # Fail-closed means EXACT match only: no trailing-slash tolerance, no
        # case folding, no trimming. A near miss must never widen a bound.
        assert cursor_bound(path) == MAX_ENDPOINT_CURSOR

    def test_the_posts_path_gets_the_ms_epoch_bound(self):
        # THE cross-module pin. `paging` cannot import `client` — that import
        # would close a cycle — so it repeats the posts path as its own
        # literal. This assertion is the only thing that will notice if the two
        # spellings ever diverge, and it is what makes the duplication safe.
        assert cursor_bound(USER_POSTS_PATH) == MAX_MS_EPOCH_CURSOR

    @pytest.mark.parametrize('path', sorted(SEARCH_PATHS))
    def test_cursor_bound_is_the_identity_on_every_search_path(self, path):
        # `_paginate_into` now calls `cursor_bound(path)` where it used to read
        # the imported constant. That refactor is behaviour-preserving on
        # `/search` if and only if this holds, so pin it directly rather than
        # inferring it from a green search test: an edit to
        # `_MS_EPOCH_CURSOR_PATHS` that accidentally caught a search path would
        # widen a security bound in silence.
        assert cursor_bound(path) == MAX_ENDPOINT_CURSOR

    def test_only_the_posts_path_is_in_the_lax_family(self):
        lax = {p for p in ENDPOINT_PATHS if cursor_bound(p) != MAX_ENDPOINT_CURSOR}
        assert lax == {USER_POSTS_PATH}

    def test_the_ms_epoch_bound_clears_a_real_cursor_without_admitting_anything(self):
        # It is a CALENDAR ceiling (2100-01-01), not a machine one: comfortably
        # above a live `max_cursor` and nowhere near 2**63, which would admit
        # any integer whatsoever and so would not be a bound at all.
        assert MAX_MS_EPOCH_CURSOR > MS
        assert MAX_MS_EPOCH_CURSOR < 2 ** 63


class TestSizeCapCoversEveryMintableShape:
    """Check 6. A NON-tautological derivation test.

    The existing pair cannot catch a wrong `_widest_endpoint()`: one restates
    the definition of `MAX_PAGE_TOKEN_CHARS`, the other compares
    `paging._worst_case()` to itself. Both stay green if the derivation
    under-estimates every real shape. So the widest mintable token per path is
    built here from the field bounds directly."""

    @pytest.mark.parametrize('path', sorted(ENDPOINT_PATHS) + [LONGEST_PATH])
    def test_the_widest_mintable_token_per_path_fits_the_cap(self, path):
        token = _widest_mintable(path)
        raw = encode(token)
        assert len(raw) <= MAX_PAGE_TOKEN_CHARS, (
            f'{path}: widest mintable token is {len(raw)} chars, past the '
            f'{MAX_PAGE_TOKEN_CHARS} cap that `decode` and the Pydantic '
            'max_length both enforce')
        # `len(encode(...)) <= cap` on its own is NOT a real check: `encode`
        # sheds the dedup window oldest-first to fit, so an under-estimated cap
        # would still satisfy it while silently dropping cross-page dedup
        # state. Measure the untrimmed wire form, and assert the window came
        # through whole — that is the property the derivation owes.
        assert len(paging._wire(token)) <= MAX_PAGE_TOKEN_CHARS, (
            f'{path}: the widest mintable token only fits by SHEDDING dedup '
            'fingerprints — the derived cap under-estimates this shape')
        survived = decode(raw, expected_query_hash=token.query_hash,
                          allowed_paths=frozenset((path,)))
        assert len(survived.seen) == MAX_SEEN_FINGERPRINTS

    def test_the_cap_is_pinned_to_its_literal_value(self):
        # `MAX_PAGE_TOKEN_CHARS` is `SearchRequest.page_token`'s Pydantic
        # `max_length`, so it is a published contract, and the existing
        # assertion for it is tautological. An over-estimating derivation (max
        # path length AND max cursor digits, which no single path can reach)
        # moves it up; an under-estimating one moves it down. Both are wrong
        # and only this catches the first.
        assert MAX_PAGE_TOKEN_CHARS == 3134

    def test_the_longest_acceptable_path_is_what_makes_the_cap_tight(self):
        # Sound AND tight: the 64-char unknown path sits exactly ON the cap,
        # every real path is comfortably under it. If this ever became a strict
        # inequality the derivation would have started over-estimating.
        assert len(encode(_widest_mintable(LONGEST_PATH))) == MAX_PAGE_TOKEN_CHARS
        assert all(len(encode(_widest_mintable(p))) < MAX_PAGE_TOKEN_CHARS
                   for p in ENDPOINT_PATHS)


# ------------------------------------------------------------ the closed 500
def _script(transport: FakeTransport, pages: list[dict]) -> None:
    """Answer each signed request with the next scripted page. Running off the
    end is an assertion failure, never a silent repeat."""
    remaining = list(pages)

    def handler(call):  # noqa: ANN001
        assert remaining, f'the loop asked for more pages than scripted: {call["path"]}'
        return remaining.pop(0)

    transport.script(handler)


def _published(page) -> EndpointState:  # noqa: ANN001
    assert len(page.endpoints) == 1
    return page.endpoints[0]


class TestAnOutOfBoundUpstreamCursorRetiresInsteadOf500ing:
    """Check on the failure the bound actually closed.

    Subtask 4 folded the bound into `_paginate_posts`' progress test:

        advanced = (0 < next_cursor <= bound
                    and (prev_cursor == POSTS_START_CURSOR or next_cursor < prev_cursor))

    Before it, an upstream `max_cursor` past the calendar ceiling became the
    published `EndpointState.cursor`, reached `encode` when the handler minted
    its continuation, and raised `ValueError` out of `run_in_executor` as a 500
    — on a request that had ALREADY served records.

    Note what is asserted and what is not: on this retire path `has_more` is
    False, so `_next_page_token` mints NO token at all (it returns None before
    reaching `encode`). The property is therefore that the PUBLISHED STATE IS
    ENCODABLE — the thing that was untrue before — not that a token is handed
    back."""

    def _client(self) -> TikTokClient:
        return TikTokClient(local_config(retries=2))

    def test_out_of_bounds_on_page_one_serves_its_records_and_retires(
        self, transport: FakeTransport, caplog
    ):
        _script(transport, [posts_reply([1, 2], max_cursor=MAX_MS_EPOCH_CURSOR + 1,
                                        has_more=True)])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            page = self._client().user_posts(user_id=UID, sec_uid=SEC, limit=50)
        assert len(transport.calls) == 1, 'retired in one signed request'
        assert [r['id'] for r in page.records] == ['1', '2'], 'records served are kept'
        assert page.has_more is False, 'an impossible cursor is not resumable'
        assert page.next_cursor is None
        assert _published(page).cursor == POSTS_START_CURSOR, (
            'the last IN-BOUNDS cursor is published, never the bad echo')
        assert 'did not advance' in caplog.text

    def test_out_of_bounds_on_page_two_keeps_the_walked_back_cursor(
        self, transport: FakeTransport, caplog
    ):
        # The harder seam: page 1 legitimately walks back, so there is real
        # resumable state to protect when page 2 answers nonsense.
        _script(transport, [
            posts_reply([1], max_cursor=MS, has_more=True),
            posts_reply([2], max_cursor=MAX_MS_EPOCH_CURSOR + 1, has_more=True),
        ])
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            page = self._client().user_posts(user_id=UID, sec_uid=SEC, limit=50)
        assert len(transport.calls) == 2
        assert [r['id'] for r in page.records] == ['1', '2']
        assert page.has_more is False
        assert _published(page).cursor == MS, (
            "page 1's in-bounds cursor survives page 2's bad echo")
        assert 'did not advance' in caplog.text

    @pytest.mark.parametrize('pages,label', [
        ([posts_reply([1, 2], max_cursor=MAX_MS_EPOCH_CURSOR + 1, has_more=True)],
         'out of bounds on page 1'),
        ([posts_reply([1], max_cursor=MS, has_more=True),
          posts_reply([2], max_cursor=MAX_MS_EPOCH_CURSOR + 1, has_more=True)],
         'out of bounds on page 2'),
    ])
    def test_the_published_state_is_always_encodable(
        self, transport: FakeTransport, pages, label
    ):
        # The 500, stated as an invariant: whatever `_paginate_posts` publishes
        # must be mintable, because a handler that has already served records
        # cannot afford a raise at mint time.
        _script(transport, pages)
        page = self._client().user_posts(user_id=UID, sec_uid=SEC, limit=50)
        raw = encode(_token(*page.endpoints, seen=page.seen))
        assert _decode(raw).endpoints[0].cursor == _published(page).cursor, label

    def test_an_in_bounds_stream_still_publishes_a_resumable_encodable_state(
        self, transport: FakeTransport
    ):
        # The complement, so the two tests above cannot pass by the loop simply
        # never publishing anything resumable.
        _script(transport, [posts_reply([1], max_cursor=MS, has_more=True)])
        page = self._client().user_posts(user_id=UID, sec_uid=SEC, limit=1)
        assert page.has_more is True
        assert _published(page).cursor == MS
        assert _decode(encode(_token(*page.endpoints, seen=page.seen))
                       ).endpoints[0].cursor == MS
