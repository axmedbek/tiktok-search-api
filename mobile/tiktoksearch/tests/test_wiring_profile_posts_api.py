"""End-to-end wiring tests for POST /profile and POST /user/posts.

These drive the REAL FastAPI app — Pydantic validation, `_to_posts_query`, the
author filter, the pool, the client, the domain→HTTP map, the token namespace
and identity health — over a synthetic identities file, with
`requests.Session.get` as the only seam. Statuses are asserted through
`app.py`'s own map rather than a copy of it here: a re-implemented map is the
hole that lets a status flip go unnoticed.

The pass exists because ~340 lines of HTTP-boundary code landed with no tests
and two reviews passed a real bug: `_authored_by` compared `author_username`,
which `flatten_video` fills as `unique_id or nickname`. A nickname is
user-settable and NOT unique, so a foreign account's video — and its `uid` /
`sec_uid` — was attributed to the requested handle. Three things follow from
that, and they shape every assertion below:

1. **Impersonation is asserted on the RESPONSE BODY**, not on `_authored_by`.
   The bug's damage was ids in a reply, so the reply is what is checked.
2. **The impersonator shares its page with the genuine account.** An
   impersonator-only page answers `count: 0` on an endpoint that returns
   nothing at all, so on its own it cannot tell a right answer from a
   right-looking one. The mixed page can: under the bug it answers `count: 2`
   with the IMPERSONATOR's ids.
3. **`author_username`'s nickname fallback and `author_unique_id`'s absence are
   pinned on ONE record, together.** Split across two tests, both keep passing
   after someone "harmonises" the two fields — which is the bug, restored.

Every app here runs `signer: local`, so signing is real in-process crypto and
the paid-signer ledger stays at zero; nothing leaves the process (see
conftest's tripwire).

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    AsgiClient,
    FakeResponse,
    FakeTransport,
    author_node,
    authored_reply,
    drive,
    empty_reply,
    identity,
    profile_node,
    reply,
    user_search_reply,
    write_config,
    write_identities,
)

from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.api.schemas import (  # noqa: E402
    DEFAULT_POSTS_PERIOD,
    MAX_SEC_UID_CHARS,
    MAX_USER_ID_CHARS,
    MAX_USERNAME_CHARS,
    POSTS_SOURCE,
    PROFILE_SOURCE,
)
from tiktoksearch.client import (  # noqa: E402
    NIL_EMPTY_SESSION,
    SEARCH_ITEM_PATH,
    SEARCH_USER_PATH,
    SEARCH_VIDEO_PATH,
)
from tiktoksearch.errors import (  # noqa: E402
    NotFound,
    PoolCode,
    PoolExhausted,
    RateLimited,
    SoftError,
    TransportError,
)
from tiktoksearch.filters import SearchFilters  # noqa: E402
from tiktoksearch.identity_manager import DEFAULT_STALE_AFTER  # noqa: E402
from tiktoksearch.mapping import flatten_user, flatten_video  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    device_handle,
    encode,
)

DEVICE_A = 'DEVA'
DEVICE_B = 'DEVB'
HANDLE = 'bakuesaz'
RISK_CONTROL_NIL = 'hit_shark'

# One below DEFAULT_STALE_AFTER: a counter parked here is visible in BOTH
# directions — one report_empty retires the identity, one report_ok clears it.
# A zero-start assertion is worthless, which is subtask 5's own recorded
# lesson: at zero, a reset and a no-op are indistinguishable.
ALMOST_STALE = DEFAULT_STALE_AFTER - 1

# The impersonator: `nickname` IS the wanted handle and there is NO `unique_id`,
# so `author_username` reads `bakuesaz` while `author_unique_id` is None.
# Its ids are what must never appear in a `/user/posts` reply for @bakuesaz.
IMPOSTOR_UID = '666'
IMPOSTOR_SEC = 'SEC666'
IMPOSTOR = author_node(nickname=HANDLE, uid=IMPOSTOR_UID, sec_uid=IMPOSTOR_SEC)
# The genuine account: `unique_id` is the handle.
GENUINE_UID = '777'
GENUINE_SEC = 'SEC777'
GENUINE = author_node(unique_id=HANDLE, nickname='Baku Esaz',
                      uid=GENUINE_UID, sec_uid=GENUINE_SEC)
# An unrelated account, for pages that are legitimately somebody else's.
FOREIGN = author_node(unique_id='someoneelse', nickname='Someone',
                      uid='9999', sec_uid='SEC9999')

# Ids a caller may pin on the request; deliberately unlike both accounts', so
# "the request won" is distinguishable from "the record supplied them".
PINNED_UID = '111'
PINNED_SEC = 'SEC111'


# ------------------------------------------------------------------ harness
def _app(tmp_path: Path, *, devices=(DEVICE_A,), **config_over):
    """The real app over a synthetic identities file, in `signer: local` mode.

    `local` and not the config-derived `rapid`, so the paid signer is never
    even CONSTRUCTED and `rapid_ledger` reads zero as the plan requires;
    signing itself still runs for real in-process."""
    ids_path = tmp_path / 'ids.json'
    write_identities(ids_path, [identity(d) for d in devices], stamp=1_700_000_100)
    config_path = tmp_path / 'config.yaml'
    write_config(config_path, signer='local', **config_over)
    return app_module.create_app(str(config_path))


def _post(app, path: str, *payloads) -> list[tuple[int, dict]]:
    """POST each payload inside ONE app lifespan, so a later call meets the
    pool state, daily cap and device the earlier one left behind."""
    async def sequence():
        async with AsgiClient(app) as client:
            return [await client.post(path, payload) for payload in payloads]

    return drive(sequence())


def _profile(app, payload) -> tuple[int, dict]:
    return _post(app, '/profile', payload)[0]


def _posts(app, payload) -> tuple[int, dict]:
    return _post(app, '/user/posts', payload)[0]


def _chain_posts(app, payload, *follow_ups) -> list[tuple[int, dict]]:
    """A real `/user/posts` continuation chain in ONE lifespan: each follow-up
    receives the previous response body and returns the next payload, so page 2
    uses the token page 1 actually minted."""
    async def sequence():
        async with AsgiClient(app) as client:
            results = [await client.post('/user/posts', payload)]
            for build in follow_ups:
                results.append(await client.post('/user/posts', build(results[-1][1])))
            return results

    return drive(sequence())


def _with_store(app, run):
    """Drive inside ONE lifespan, handing `run` the live IdentityStore and pool.

    Both are built BY the lifespan, so a health assertion cannot reach them
    from outside it."""
    async def sequence():
        async with AsgiClient(app) as client:
            return await run(client, app.state.identities, app.state.pool)

    return drive(sequence())


def _spy_reports(pool, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Ledger of `_report` calls: one `ok=` per report actually emitted.

    A second instrument beside the consecutive-empty counter, because the
    counter alone cannot separate "reported ok" from "not reported at all" —
    and NEUTRAL means the latter. The real `_report` still runs."""
    original = pool._report
    emitted: list[bool] = []

    def spy(slot, *, ok: bool) -> None:
        emitted.append(ok)
        original(slot, ok=ok)

    monkeypatch.setattr(pool, '_report', spy)
    return emitted


def _park_almost_stale(store, device: str = DEVICE_A) -> None:
    """Drive the identity's consecutive-empty count to DEFAULT_STALE_AFTER - 1."""
    for _ in range(ALMOST_STALE):
        store.report_empty(device)
    assert store.get(device).consecutive_empty == ALMOST_STALE


# -------------------------------------------------------- transport scripting
def _resolve_reply(nickname: str = HANDLE) -> dict:
    """The user-search answer a `/user/posts` FIRST PAGE resolves the handle
    against, for the account's display name.

    `nickname` is the handle by default, so `_posts_keywords` derives the
    handle keyword ALONE — one search per page, which is what every assertion
    in this file about pages, tokens and cap units was written against. A test
    about the display-name keyword passes a different nickname."""
    return user_search_reply([profile_node(nickname=nickname)])


def _by_path(transport: FakeTransport, mapping) -> None:
    """Answer each signed path from `mapping` (a reply dict, or a callable over
    the recorded call). A keyword search drives BOTH merged video endpoints, so
    a posts page normally needs an answer for each — and a first page resolves
    the handle first, so the user-search answer is supplied by default and may
    be overridden."""
    answers = {SEARCH_USER_PATH: _resolve_reply(), **mapping}

    def handler(call):
        answer = answers[call['path']]
        return answer(call) if callable(answer) else dict(answer)

    transport.script(handler)


def _single_page(transport: FakeTransport, items) -> None:
    """One posts page, TERMINAL on both merged endpoints so no token is minted.

    The Videos-tab endpoint echoes the SAME items, which the shared dedup
    window drops — so the page's records are exactly `items`, and neither
    endpoint answers the sessionless empty that would read as risk-control."""
    _by_path(transport, {
        SEARCH_VIDEO_PATH: authored_reply(items, cursor=len(items), has_more=False),
        SEARCH_ITEM_PATH: authored_reply(items, cursor=len(items), has_more=False,
                                         key='search_item_list'),
    })


def _pages(transport: FakeTransport, *replies) -> None:
    """Answer the Nth signed SEARCH request with the Nth reply (the last one
    repeats).

    Paired with `limit=1`, where one posts page drives exactly ONE endpoint for
    exactly one inner page — so search request N is page N of the chain, and
    the Videos-tab endpoint is never opened.

    The handle RESOLVE is answered separately and does NOT consume an index: it
    is not part of the page chain, and letting it eat `replies[0]` would shift
    every page of every chain by one."""
    served = {'n': 0}

    def handler(call):
        if call['path'] == SEARCH_USER_PATH:
            return _resolve_reply()
        index = min(served['n'], len(replies) - 1)
        served['n'] += 1
        answer = replies[index]
        return answer(call) if callable(answer) else dict(answer)

    transport.script(handler)


def _blocks(call) -> dict:
    """A healthy `/search` stream: ten fresh ids per window, cursor advancing."""
    offset = int(call['offset'] or 0)
    first = offset + 1
    return reply(range(first, first + 10), cursor=offset + 10, has_more=True)


# ------------------------------------------------------------ token minting
def _posts_token(*endpoints: EndpointState, device: str = DEVICE_A,
                 handle: str = HANDLE) -> str:
    """A `/user/posts` token this server instance would accept — minted through
    the app's OWN `_posts_query_hash`, exactly as `_mint_posts_token` does,
    under the DEFAULT `period` (the window a request that names none searches).
    The window is part of the hash, so a token minted under another one is
    rejected — which is what stops a token resuming a different query than it
    was minted for."""
    return encode(PageToken(version=TOKEN_VERSION,
                            query_hash=app_module._posts_query_hash(
                                handle, SearchFilters(publish_time=DEFAULT_POSTS_PERIOD)),
                            device_handle=device_handle(device),
                            endpoints=endpoints or (_resumable_primary(),)))


def _resumable_primary(cursor: int = 30) -> EndpointState:
    """A live session on the general endpoint: carries a search_id, so the next
    request goes out WITH one and an empty answer is a tail, not risk-control."""
    return EndpointState(SEARCH_VIDEO_PATH, cursor, 'SIDP', True, True)


def _retired_secondary() -> EndpointState:
    return EndpointState(SEARCH_ITEM_PATH, 0, '', False, True)


def _ids(body: dict) -> list[str]:
    return [record['id'] for record in body['results']]


# =========================================================== 1-4, 7: identity
class TestTheAuthorFilterComparesTheIdentityFieldNotTheDisplayField:
    """The bug this whole pass exists for, asserted on the response body.

    `flatten_video` fills `author_username` from `unique_id or nickname`;
    `_authored_by` must compare `author_unique_id`, which has no nickname
    fallback. Otherwise any account whose NICKNAME equals the wanted handle is
    served as that handle, ids included."""

    def test_a_nickname_only_impersonator_is_absent_from_the_whole_body(
            self, tmp_path, transport, rapid_ledger):
        # The page is terminal, so `page_token` is null and "the whole body"
        # really is every byte the caller receives — no opaque token to hide an
        # id inside.
        _single_page(transport, [('1', IMPOSTOR)])
        status, body = _posts(_app(tmp_path), {'username': f'@{HANDLE}'})
        assert status == 200
        assert body['count'] == 0
        assert body['results'] == []
        assert body['page_token'] is None
        # Not just the records: `_posts_ids` must not adopt the impersonator's
        # ids as this handle's either.
        assert body['user_id'] is None
        assert body['sec_uid'] is None
        serialised = json.dumps(body)
        assert IMPOSTOR_UID not in serialised
        assert IMPOSTOR_SEC not in serialised
        assert rapid_ledger == []

    def test_an_impersonator_beside_the_genuine_account_yields_only_the_genuine(
            self, tmp_path, transport):
        # THE discriminating case. `count: 0` above can pass on an endpoint that
        # returns nothing at all; this cannot. The impersonator is listed FIRST,
        # so a filter that matched it would also hand back ITS ids via
        # `_posts_ids`, which reads the first KEPT record.
        _single_page(transport, [('1', IMPOSTOR), ('2', GENUINE)])
        status, body = _posts(_app(tmp_path), {'username': f'@{HANDLE}'})
        assert status == 200
        assert body['count'] == 1
        assert _ids(body) == ['2']
        assert body['results'][0]['author_id'] == GENUINE_UID
        assert body['results'][0]['author_sec_uid'] == GENUINE_SEC
        # The reported ids are the genuine account's, not the first record's.
        assert body['user_id'] == GENUINE_UID
        assert body['sec_uid'] == GENUINE_SEC
        serialised = json.dumps(body)
        assert IMPOSTOR_UID not in serialised
        assert IMPOSTOR_SEC not in serialised

    @pytest.mark.parametrize('unusable,label', [
        (author_node(uid=IMPOSTOR_UID, sec_uid=IMPOSTOR_SEC), 'absent'),
        (author_node(unique_id='', uid=IMPOSTOR_UID, sec_uid=IMPOSTOR_SEC), 'empty'),
        (author_node(unique_id=[HANDLE], uid=IMPOSTOR_UID, sec_uid=IMPOSTOR_SEC),
         'non-string'),
    ])
    def test_a_record_with_no_usable_handle_is_dropped_never_matched(
            self, tmp_path, transport, unusable, label):
        # An empty handle must not compare equal to a wanted handle, and a
        # non-string must not crash the filter into a 500. Dropped, either way.
        _single_page(transport, [('1', unusable)])
        status, body = _posts(_app(tmp_path), {'username': HANDLE})
        assert status == 200, label
        assert body['count'] == 0, label
        assert IMPOSTOR_UID not in json.dumps(body), label

    def test_the_handle_comparison_is_case_insensitive(self, tmp_path, transport):
        # TikTok handles are case-insensitive, so a node spelled `BakuEsaz` IS
        # `@bakuesaz` and must be KEPT — the filter fails closed, and this is
        # the boundary where failing closed would be wrong.
        mixed = author_node(unique_id='BakuEsaz', uid=GENUINE_UID,
                            sec_uid=GENUINE_SEC)
        _single_page(transport, [('1', mixed)])
        status, body = _posts(_app(tmp_path), {'username': f'@{HANDLE}'})
        assert status == 200
        assert body['count'] == 1
        assert body['user_id'] == GENUINE_UID


class TestTheFlattenerContract:
    """The two flatteners are pure, and `/search` shares them with
    `/user/posts` — so their key sets are a published contract."""

    # The exact tuple, ORDER INCLUDED, so a later edit that renames, retypes,
    # reorders or drops a key fails loudly instead of silently changing what
    # every `/search` caller receives.
    VIDEO_KEYS = ('id', 'description', 'create_time', 'author_username',
                  'author_unique_id', 'author_id', 'author_sec_uid',
                  'region_code', 'view_count', 'like_count', 'comment_count',
                  'share_count', 'hashtags', 'music_id', 'music_title',
                  'duration', 'source_term')
    USER_KEYS = ('type', 'id', 'username', 'display_name', 'follower_count',
                 'following_count', 'aweme_count', 'signature', 'region_code',
                 'verified', 'user_id', 'sec_uid', 'source_term')

    def _video(self) -> dict:
        raw = {'aweme_id': '1', 'desc': 'd', 'create_time': 1_700_000_000,
               'author': GENUINE, 'statistics': {'play_count': 5, 'digg_count': 4,
                                                 'comment_count': 3, 'share_count': 2},
               'region': 'AZ', 'video': {'duration': 1000},
               'music': {'id': 'M', 'title': 'T'},
               'cha_list': [{'cha_name': 'tag'}]}
        record = flatten_video(raw, 'search:x')
        assert record is not None
        return record

    def test_flatten_video_key_tuple_is_pinned(self):
        assert tuple(self._video().keys()) == self.VIDEO_KEYS

    def test_flatten_video_key_types_are_pinned(self):
        record = self._video()
        for key in ('id', 'description', 'author_username', 'author_unique_id',
                    'author_id', 'author_sec_uid', 'region_code', 'music_id',
                    'music_title', 'source_term', 'create_time'):
            assert isinstance(record[key], str), key
        for key in ('view_count', 'like_count', 'comment_count', 'share_count',
                    'duration'):
            assert isinstance(record[key], int), key
        assert isinstance(record['hashtags'], list)

    def test_flatten_user_key_tuple_is_pinned(self):
        record = flatten_user(profile_node(signature='bio', region='AZ'), 'user:x')
        assert record is not None
        assert tuple(record.keys()) == self.USER_KEYS
        assert isinstance(record['verified'], bool)
        assert record['sec_uid'] == GENUINE_SEC
        assert record['user_id'] == GENUINE_UID

    def test_the_identity_field_is_none_while_the_display_field_falls_back(self):
        # ONE record, BOTH assertions. Split into two tests, this pair keeps
        # passing after someone "harmonises" the two fields — which is exactly
        # the bug that shipped: `author_unique_id` gaining a nickname fallback
        # makes an impersonator match, and `author_username` losing its
        # fallback silently empties a display field every caller reads.
        record = flatten_video({'aweme_id': '1', 'author': IMPOSTOR}, 'search:x')
        assert record is not None
        assert record['author_username'] == HANDLE      # display: nickname
        assert record['author_unique_id'] is None       # identity: no fallback
        # And the ids really are on the record, so the filter — not a missing
        # payload — is what keeps them out of the reply.
        assert record['author_id'] == IMPOSTOR_UID
        assert record['author_sec_uid'] == IMPOSTOR_SEC


# ============================================================ 5, 8, 9: paging
class TestPostsIdsProvenance:
    """`_posts_ids`: what the caller supplied wins, so the pair stays stable
    across pages; otherwise the ids come off the first KEPT record."""

    def test_supplied_ids_win_over_the_records(self, tmp_path, transport):
        _single_page(transport, [('1', GENUINE)])
        status, body = _posts(_app(tmp_path), {
            'username': HANDLE, 'user_id': PINNED_UID, 'sec_uid': PINNED_SEC})
        assert status == 200
        assert body['count'] == 1
        assert body['user_id'] == PINNED_UID
        assert body['sec_uid'] == PINNED_SEC

    def test_supplied_ids_stay_stable_across_pages(self, tmp_path, transport):
        # Page 2 keeps NOTHING (a page of other authors), which is precisely
        # the case the request-supplied ids exist for: the ids must not blink
        # to null just because one page filtered down to zero.
        _pages(transport,
               authored_reply([('1', GENUINE)], cursor=10, has_more=True),
               authored_reply([('2', FOREIGN)], cursor=20, has_more=True))
        page = {'username': HANDLE, 'limit': 1,
                'user_id': PINNED_UID, 'sec_uid': PINNED_SEC}
        (first_status, first), (second_status, second) = _chain_posts(
            _app(tmp_path), page,
            lambda body: {**page, 'page_token': body['page_token']})
        assert (first_status, second_status) == (200, 200)
        assert first['count'] == 1 and second['count'] == 0
        assert (first['user_id'], first['sec_uid']) == (PINNED_UID, PINNED_SEC)
        assert (second['user_id'], second['sec_uid']) == (PINNED_UID, PINNED_SEC)


class TestAnAllFilteredPageIsStillAResumablePage:
    def test_count_zero_with_has_more_and_a_token(self, tmp_path, transport):
        # `count` is post-filter and `has_more` is pre-filter, so this shape is
        # a normal answer, not a contradiction: an unlucky page can be entirely
        # other authors while the underlying search has plenty left. It MUST
        # still mint a token, or a caller stops paging one page early and the
        # account's posts are unreachable.
        _pages(transport, authored_reply([('1', FOREIGN)], cursor=10, has_more=True))
        status, body = _posts(_app(tmp_path), {'username': HANDLE, 'limit': 1})
        assert status == 200
        assert body['count'] == 0
        assert body['results'] == []
        assert body['has_more'] is True
        assert body['page_token'] is not None


class TestPostsContinuation:
    def test_page_two_round_trips_pins_the_device_and_seeds_seen(
            self, tmp_path, transport):
        # Page 2 re-serves id '1' alongside a fresh '2'. The token's `seen`
        # window must drop '1', or a caller concatenating pages shows it twice.
        _pages(transport,
               authored_reply([('1', GENUINE)], cursor=10, has_more=True),
               authored_reply([('1', GENUINE), ('2', GENUINE)], cursor=20,
                              has_more=True))
        page = {'username': HANDLE, 'limit': 1}
        app = _app(tmp_path, devices=(DEVICE_A, DEVICE_B))
        (first_status, first), (second_status, second) = _chain_posts(
            app, page, lambda body: {**page, 'page_token': body['page_token']})
        assert (first_status, second_status) == (200, 200)
        assert _ids(first) == ['1']
        # Seeded dedup: '1' is gone, only the fresh record comes back.
        assert _ids(second) == ['2']
        # A search session lives on ONE device, so page 2 is pinned to the
        # device that served page 1's SEARCH — asserted on the label AND on the
        # device_id that actually signed. Read off the SEARCH calls and off the
        # last `+`-joined label, never off every call the page made: page 1's
        # handle resolve is an INDEPENDENT pooled call and on a two-device pool
        # lands on the other device, which is why `device` is `+`-joined at all.
        assert second['device'] == first['device'].split('+')[-1]
        assert len({call['device'] for call in transport.calls
                    if call['path'] in (SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH)}) == 1

    def test_the_posts_token_is_case_insensitive_on_the_handle(
            self, tmp_path, transport):
        # `_posts_query_hash` lower-cases its input, so a caller that varies
        # capitalisation between pages is not 422'd. Both directions.
        _pages(transport,
               authored_reply([('1', GENUINE)], cursor=10, has_more=True),
               authored_reply([('2', GENUINE)], cursor=20, has_more=True))
        app = _app(tmp_path)
        (_, first), (second_status, _) = _chain_posts(
            app, {'username': '@BakuEsaz', 'limit': 1},
            lambda body: {'username': f'@{HANDLE}', 'limit': 1,
                          'page_token': body['page_token']})
        assert second_status == 200

    def test_the_posts_token_is_case_insensitive_in_the_other_direction(
            self, tmp_path, transport):
        _pages(transport,
               authored_reply([('1', GENUINE)], cursor=10, has_more=True),
               authored_reply([('2', GENUINE)], cursor=20, has_more=True))
        (_, first), (second_status, _) = _chain_posts(
            _app(tmp_path), {'username': f'@{HANDLE}', 'limit': 1},
            lambda body: {'username': '@BakuEsaz', 'limit': 1,
                          'page_token': body['page_token']})
        assert second_status == 200

    def test_a_posts_token_replayed_against_a_different_handle_is_422(
            self, tmp_path, transport):
        # Case-insensitivity must not become handle-insensitivity: the token is
        # still bound to ONE account.
        _pages(transport, authored_reply([('1', GENUINE)], cursor=10, has_more=True))
        (_, first), (second_status, _) = _chain_posts(
            _app(tmp_path), {'username': f'@{HANDLE}', 'limit': 1},
            lambda body: {'username': 'someoneelse', 'limit': 1,
                          'page_token': body['page_token']})
        assert second_status == 422


# ================================================= 10, 11: the token namespace
class TestTheTwoEndpointsTokensAreNotInterchangeable:
    """The two endpoints resume the same upstream stream but filter and shape
    their pages differently, so a token that silently works on both is a
    contract nobody chose. `POSTS_QUERY_KIND` namespaces the hash."""

    def test_a_search_token_is_rejected_at_user_posts(self, tmp_path, transport):
        transport.script(_blocks)
        app = _app(tmp_path)

        async def sequence():
            async with AsgiClient(app) as client:
                _, search_body = await client.post(
                    '/search', {'type': 'keyword', 'query': HANDLE, 'limit': 10})
                assert search_body['page_token'] is not None
                return await client.post('/user/posts', {
                    'username': HANDLE, 'limit': 10,
                    'page_token': search_body['page_token']})

        status, _ = drive(sequence())
        assert status == 422

    def test_a_posts_token_is_rejected_at_search(self, tmp_path, transport):
        _pages(transport, authored_reply([('1', GENUINE)], cursor=10, has_more=True))
        app = _app(tmp_path)

        async def sequence():
            async with AsgiClient(app) as client:
                _, posts_body = await client.post(
                    '/user/posts', {'username': HANDLE, 'limit': 1})
                assert posts_body['page_token'] is not None
                return await client.post('/search', {
                    'type': 'keyword', 'query': HANDLE, 'limit': 10,
                    'page_token': posts_body['page_token']})

        status, _ = drive(sequence())
        assert status == 422

    def test_a_user_search_path_token_is_rejected_at_user_posts(
            self, tmp_path, transport):
        # This is what makes the NARROWED `POSTS_TOKEN_PATHS` safe. The path
        # inside a token reaches a SIGNED TikTok URL, so the allow-list is the
        # only thing standing between a token and a path this endpoint would
        # never mint. Correctly hashed and correctly signed — only the path is
        # foreign, so the 422 can come from nothing else.
        token = _posts_token(EndpointState(SEARCH_USER_PATH, 30, 'SIDU', True, True))
        status, _ = _posts(_app(tmp_path), {
            'username': HANDLE, 'limit': 10, 'page_token': token})
        assert status == 422
        assert transport.calls == []


# ====================================== 12, 13: identity health on /user/posts
class TestPostsIdentityHealth:
    """Risk-control must reach `IdentityStore`; an ordinary empty must not.

    Every NEUTRAL assertion starts from a NON-ZERO consecutive-empty count.
    At zero a reset and a no-op are indistinguishable, which is how
    `_REPORT_BY_VERDICT[NEUTRAL] = True` survived a 396-test suite."""

    def test_hit_shark_is_502_and_reports_the_empty(
            self, tmp_path, transport, monkeypatch):
        # Never a 200 with count: 0 — anti-block invariant (a) — and the empty
        # must be REPORTED, or invariant (b) can never retire a cold identity.
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        app = _app(tmp_path)

        async def run(client, store, pool):
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/user/posts',
                                             {'username': HANDLE, 'limit': 10})
            return status, body, reports, store.get(DEVICE_A).consecutive_empty

        status, body, reports, counter = _with_store(app, run)
        assert status == 502
        assert reports == [False]
        assert counter == 1

    def test_a_session_tail_is_200_and_reports_neither_ok_nor_empty(
            self, tmp_path, transport, monkeypatch):
        # A private / zero-post / search-exhausted handle legitimately returns
        # nothing, and three of those must NOT retire the only warm identity.
        # NEUTRAL therefore means "not reported at all" — asserted from a
        # counter parked one below DEFAULT_STALE_AFTER, so an EMPTY verdict
        # would retire the identity and an OK verdict would clear the counter.
        transport.script(lambda call: empty_reply(nil=NIL_EMPTY_SESSION))
        token = _posts_token(_resumable_primary(), _retired_secondary())
        app = _app(tmp_path)

        async def run(client, store, pool):
            _park_almost_stale(store)
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/user/posts', {
                'username': HANDLE, 'limit': 10, 'page_token': token})
            return status, body, reports, store.get(DEVICE_A).consecutive_empty

        status, body, reports, counter = _with_store(app, run)
        assert status == 200
        assert body['count'] == 0
        assert reports == []                    # neither report_ok nor report_empty
        assert counter == ALMOST_STALE          # not incremented, not cleared
        assert counter < DEFAULT_STALE_AFTER    # so the identity is still usable


# ==================================================== 14: /profile's branches
class TestProfileBranches:
    """All four outcomes, asserting the identity REPORT and not only the status.
    Which branch charged a healthy identity is invisible in the status code."""

    def test_an_exact_match_is_200_in_one_signed_request(
            self, tmp_path, transport, monkeypatch, rapid_ledger):
        transport.script(lambda call: user_search_reply([profile_node()]))
        app = _app(tmp_path)

        async def run(client, store, pool):
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/profile', {'username': f'@{HANDLE}'})
            return status, body, reports

        status, body, reports = _with_store(app, run)
        assert status == 200
        # ONE signed request, on the user-search path — no second upstream call.
        assert len(transport.calls) == 1
        assert transport.paths == [SEARCH_USER_PATH]
        assert body['username'] == HANDLE
        assert body['user_id'] == GENUINE_UID
        assert body['sec_uid'] == GENUINE_SEC
        # Structurally absent from the user-search node, so null on this path —
        # not "this account has no bio / region".
        assert body['signature'] is None
        assert body['region_code'] is None
        assert body['source'] == PROFILE_SOURCE
        assert body['heart_count'] == 41_921_593   # total_favorited, not favoriting
        assert body['private'] is False
        assert reports == [True]
        assert rapid_ledger == []

    def test_no_exact_match_is_404_with_no_identity_report(
            self, tmp_path, transport, monkeypatch):
        # Live TikTok answers a nonsense handle with a POPULATED list of fuzzy
        # neighbours, so this — not the empty list — is the branch a bad handle
        # reaches. The warm identity did nothing wrong and must not be charged.
        transport.script(lambda call: user_search_reply([
            profile_node(unique_id='notzd9', uid='1'),
            profile_node(unique_id='notarealuser9', uid='2')]))
        app = _app(tmp_path)

        async def run(client, store, pool):
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/profile', {'username': f'@{HANDLE}'})
            return status, body, reports, store.get(DEVICE_A).consecutive_empty

        status, body, reports, counter = _with_store(app, run)
        assert status == 404
        assert body['detail'] == app_module.NO_SUCH_USER_DETAIL
        assert reports == []
        assert counter == 0
        # No retry budget spent: re-signing cannot make a deleted user exist.
        assert len(transport.calls) == 1

    def test_an_empty_user_list_is_502_and_reports_the_empty(
            self, tmp_path, transport, monkeypatch):
        # An empty item list on a sessionless first page IS the hit_shark
        # signature, and nothing in the payload tells it apart from a
        # nonexistent user. 404 here would launder a shadow-block AND throw
        # away the only signal that the warm identity has gone cold.
        transport.script(lambda call: user_search_reply([]))
        app = _app(tmp_path)

        async def run(client, store, pool):
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/profile', {'username': f'@{HANDLE}'})
            return status, body, reports, store.get(DEVICE_A).consecutive_empty

        status, body, reports, counter = _with_store(app, run)
        assert status == 502
        assert reports == [False]
        assert counter == 1

    def test_a_uid_less_node_is_502_via_transport_error_with_no_report(
            self, tmp_path, transport, monkeypatch):
        # A node that matches on handle but carries no `uid` flattens to None.
        # It must not surface as a 200 with a null profile, and it must not be
        # a SoftError either: the reply arrived and parsed, so charging a
        # healthy identity for a merely ODD payload is wrong. TransportError is
        # the only 502-mapping class outside `run_call`'s `except SoftError`.
        transport.script(lambda call: user_search_reply([profile_node(uid=None)]))
        app = _app(tmp_path)

        async def run(client, store, pool):
            reports = _spy_reports(pool, monkeypatch)
            status, body = await client.post('/profile', {'username': f'@{HANDLE}'})
            return status, body, reports, store.get(DEVICE_A).consecutive_empty

        status, body, reports, counter = _with_store(app, run)
        assert status == 502
        assert reports == []            # the discriminator against SoftError
        assert counter == 0
        # And distinguishable from the empty-list 502 in the reply itself,
        # which is what tells the two 502s apart in an incident.
        assert 'empty' not in body['detail']
        assert len(transport.calls) == 1


# =============================================== 15: /search is not regressed
# One case per domain exception class, at fan_out=1 and fan_out>1. `/search`
# was folded into the shared `_domain_errors` map, and the fold is only
# behaviour-preserving if every class still maps where it did.
DOMAIN_STATUSES = [
    (PoolExhausted('capped', code=PoolCode.CAP), 429),
    (PoolExhausted('busy', code=PoolCode.BUSY), 503),
    (PoolExhausted('gone', code=PoolCode.GONE), 503),
    (PoolExhausted('stale', code=PoolCode.STALE), 503),
    (RateLimited('rate limited'), 429),
    (NotFound('no such user'), 404),
    (SoftError('empty search result (hit_shark)'), 502),
    (TransportError('HTTP 500 len 0'), 502),
]


class TestSearchIsUnchangedByTheDomainErrorFold:
    """`//search`'s statuses, one case per exception class, on BOTH branches.

    The classes are INJECTED at the pool boundary rather than contrived from
    upstream replies, because that is the only way to cover the full class list
    — `NotFound` is unreachable on the search path by construction, and it is
    precisely the class a hand-copied error map drops."""

    @pytest.mark.parametrize('exc,expected', DOMAIN_STATUSES)
    def test_fan_out_one_maps_every_domain_class(self, tmp_path, transport,
                                                 monkeypatch, exc, expected):
        app = _app(tmp_path)

        async def run(client, store, pool):
            def boom(*a, **k):
                raise exc

            monkeypatch.setattr(pool, 'run', boom)
            return await client.post('/search', {'type': 'keyword',
                                                 'query': 'ocean', 'fan_out': 1})

        status, _ = _with_store(app, run)
        assert status == expected

    @pytest.mark.parametrize('exc,expected', DOMAIN_STATUSES)
    def test_fan_out_above_one_maps_every_domain_class(self, tmp_path, transport,
                                                       monkeypatch, exc, expected):
        app = _app(tmp_path, devices=(DEVICE_A, DEVICE_B))

        async def run(client, store, pool):
            def boom(*a, **k):
                raise exc

            monkeypatch.setattr(pool, 'run_merged', boom)
            return await client.post('/search', {'type': 'keyword',
                                                 'query': 'ocean', 'fan_out': 2})

        status, _ = _with_store(app, run)
        assert status == expected

    def test_real_risk_control_is_still_502_on_both_branches(self, tmp_path,
                                                             transport):
        # The injected cases above pin the map; this pins that the map is
        # reached by a REAL upstream reply, on each branch.
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        app = _app(tmp_path, devices=(DEVICE_A, DEVICE_B))
        single, merged = _post(app, '/search',
                               {'type': 'keyword', 'query': 'ocean', 'fan_out': 1},
                               {'type': 'keyword', 'query': 'ocean', 'fan_out': 2})
        assert single[0] == 502
        assert merged[0] == 502

    def test_a_real_rate_limit_is_still_429(self, tmp_path, transport):
        transport.script(lambda call: FakeResponse({'status_code': 0}, status=429))
        status, _ = _post(_app(tmp_path), '/search',
                          {'type': 'keyword', 'query': 'ocean', 'fan_out': 1})[0]
        assert status == 429

    def test_a_real_transport_failure_is_still_502(self, tmp_path, transport):
        transport.script(lambda call: FakeResponse({}, status=500))
        status, _ = _post(_app(tmp_path), '/search',
                          {'type': 'keyword', 'query': 'ocean', 'fan_out': 1})[0]
        assert status == 502

    def test_a_healthy_search_still_serves_and_mints_a_token(self, tmp_path,
                                                             transport):
        transport.script(_blocks)
        status, body = _post(_app(tmp_path), '/search',
                             {'type': 'keyword', 'query': 'ocean', 'limit': 10,
                              'fan_out': 1})[0]
        assert status == 200
        assert body['count'] == 10
        assert body['page_token'] is not None


# ======================================================= 16: the schema pin
class TestTheGeneratedSchemaMarksTheInterimFieldsRequired:
    """Taken off `app.openapi()`, NOT off the model.

    A Pydantic field WITH a default is omitted from OpenAPI's `required` list,
    so a generated client types it optional-and-possibly-missing — the
    doc-footnote status `source` and `complete` exist to escape. Asserting on
    the model would pass either way, which is why this reads the spec."""

    def _required(self, app, schema: str) -> list[str]:
        return app.openapi()['components']['schemas'][schema]['required']

    def test_profile_response_requires_source(self, tmp_path):
        assert 'source' in self._required(_app(tmp_path), 'ProfileResponse')

    def test_user_posts_response_requires_source_and_complete(self, tmp_path):
        required = self._required(_app(tmp_path), 'UserPostsResponse')
        assert 'source' in required
        assert 'complete' in required

    def test_the_interim_values_are_what_the_handlers_actually_send(
            self, tmp_path, transport):
        # The pin above is worthless if the handler sends something else.
        _single_page(transport, [('1', GENUINE)])
        status, body = _posts(_app(tmp_path), {'username': HANDLE})
        assert status == 200
        assert body['source'] == POSTS_SOURCE
        assert body['complete'] is False


# ============================================== 17: one cap unit per pooled call
class TestTheDailyCapIsChargedPerPooledCall:
    """The cap is charged per `run_call`, so a handler that stacked several
    logical calls into one would ride a single cap unit for several signed
    requests and break the plan's own cost model.

    `/user/posts` therefore spends one unit for its handle resolve plus one per
    searched keyword, and REPORTS that as `cap_units` — the caller is paying for
    coverage and must be able to see the price."""

    def test_profile_spends_exactly_one_cap_unit(self, tmp_path, transport,
                                                 rapid_ledger):
        transport.script(lambda call: user_search_reply([profile_node()]))
        app = _app(tmp_path, daily_request_cap_per_device=2)
        payload = {'username': HANDLE}
        first, second, third = _post(app, '/profile', payload, payload, payload)
        assert (first[0], second[0]) == (200, 200)      # two units, two calls
        assert third[0] == 429                          # the cap, not a 503
        assert third[1]['detail']
        assert rapid_ledger == []

    def test_a_posts_page_spends_one_unit_per_pooled_call_and_says_so(
            self, tmp_path, transport, rapid_ledger):
        # The chain is priced 2 + 1 + 1: page 1 pays for the handle resolve AND
        # its one keyword, while a continuation resolves nothing and searches
        # the handle alone. A cap of 4 is therefore spent exactly at page 3, and
        # page 4 is the 429 — which pins the arithmetic from both ends.
        _pages(transport,
               authored_reply([('1', GENUINE)], cursor=10, has_more=True),
               authored_reply([('2', GENUINE)], cursor=20, has_more=True),
               authored_reply([('3', GENUINE)], cursor=30, has_more=True),
               authored_reply([('4', GENUINE)], cursor=40, has_more=True))
        app = _app(tmp_path, daily_request_cap_per_device=4)
        page = {'username': HANDLE, 'limit': 1}


        def follow(body: dict) -> dict:
            return {**page, 'page_token': body['page_token']}

        results = _chain_posts(app, page, follow, follow, follow)
        assert [status for status, _ in results] == [200, 200, 200, 429]
        assert [body['cap_units'] for status, body in results
                if status == 200] == [2, 1, 1]
        assert rapid_ledger == []


# ==================================================== 18: the 422 boundary
# Every one of these must be refused BEFORE a signed request, because the
# handle reaches a signed TikTok URL as the search keyword.
BAD_HANDLES = [
    ('', 'empty'),
    ('   ', 'whitespace only'),
    ('@', 'a bare @ normalises to empty'),
    ('@@bob', 'a double @ is a typo, not a handle'),
    ('a' * (MAX_USERNAME_CHARS + 1), 'over the length bound'),
    ('bad-handle', 'hyphens: TikTok handles have none'),
    (123, 'a non-string'),
]


class TestTheHandleBoundary:
    """`_HandleRequest` normalises then bounds: strip whitespace, remove ONE
    leading `@`, then check length and charset."""

    @pytest.mark.parametrize('handle,label', BAD_HANDLES)
    def test_profile_refuses_a_bad_handle_before_signing(self, tmp_path,
                                                         transport, handle, label):
        status, _ = _profile(_app(tmp_path), {'username': handle})
        assert status == 422, label
        assert transport.calls == [], label

    @pytest.mark.parametrize('handle,label', BAD_HANDLES)
    def test_user_posts_refuses_a_bad_handle_before_signing(self, tmp_path,
                                                            transport, handle, label):
        status, _ = _posts(_app(tmp_path), {'username': handle})
        assert status == 422, label
        assert transport.calls == [], label

    @pytest.mark.parametrize('handle', ['@bakuesaz', ' bakuesaz ', 'bakuesaz',
                                        'baku.esaz_1'])
    def test_the_accepted_spellings_normalise_to_one_request(self, tmp_path,
                                                             transport, handle):
        transport.script(lambda call: user_search_reply([
            profile_node(unique_id=handle.strip().removeprefix('@'))]))
        status, body = _profile(_app(tmp_path), {'username': handle})
        assert status == 200
        assert body['username'] == handle.strip().removeprefix('@')

    @pytest.mark.parametrize('limit', [0, 301])
    def test_an_out_of_range_limit_is_422(self, tmp_path, transport, limit):
        status, _ = _posts(_app(tmp_path), {'username': HANDLE, 'limit': limit})
        assert status == 422
        assert transport.calls == []

    def test_ids_without_a_username_are_a_missing_field_not_a_lookup(
            self, tmp_path, transport):
        # There is no id→handle lookup anywhere in the tree, so an id-only
        # request cannot be honoured — and it must fail as `username: missing`
        # rather than as some other error that reads like a server fault.
        status, body = _profile(_app(tmp_path),
                                {'user_id': PINNED_UID, 'sec_uid': PINNED_SEC})
        assert status == 422
        assert any(item['type'] == 'missing' and item['loc'][-1] == 'username'
                   for item in body['detail']), body['detail']
        assert transport.calls == []

    def test_user_posts_also_requires_the_username_alongside_the_ids(
            self, tmp_path, transport):
        status, body = _posts(_app(tmp_path),
                              {'user_id': PINNED_UID, 'sec_uid': PINNED_SEC})
        assert status == 422
        assert any(item['type'] == 'missing' and item['loc'][-1] == 'username'
                   for item in body['detail']), body['detail']
        assert transport.calls == []


# The two OPTIONAL ids on `/user/posts`. They are echo-only on this contract —
# nothing builds a request from them — which is exactly why they were left
# untested, and exactly why they are worth a test: an unbounded string a
# caller can place in a response body is a channel, and `client.profile` (the
# upgrade path already in the tree) puts both back into a SIGNED URL the day a
# capture settles the param set. The bound must already be at the boundary
# then, not added in the same commit that revives the path.
# The two bounds as ABSOLUTE numbers, not as references to the constants. A
# case built as `'9' * (MAX_USER_ID_CHARS + 1)` moves WITH the constant, so it
# is rejected at any limit and the assertion holds under every widening — the
# same self-referential no-op a `len(encode(worst)) <= cap` size check is.
# Pinned literally: raising either ceiling now fails here and is a deliberate,
# re-pinned decision.
USER_ID_LIMIT = 32
SEC_UID_LIMIT = 200

BAD_USER_IDS = [
    ('', 'empty'),
    ('abc', 'non-numeric'),
    ('12a', 'partly numeric'),
    ('-1', 'a sign is not a digit'),
    ('1.0', 'a decimal point is not a digit'),
    (' 1', 'no normalisation happens here, unlike the handle'),
    ('1 ', 'trailing space'),
    ('9' * (USER_ID_LIMIT + 1), 'one over the length bound'),
    ('9' * 64, 'far over the length bound'),
    ('777; DROP', 'punctuation'),
    ('७७७', 'non-ASCII digits'),
    (777, 'a non-string'),
]

BAD_SEC_UIDS = [
    ('', 'empty'),
    ('SEC 777', 'a space'),
    ('SEC/777', 'base64 rather than base64url'),
    ('SEC+777', 'base64 rather than base64url'),
    ('SEC=777', 'padding is not in the charset'),
    ('SEC.777', 'a dot'),
    ('S' * (SEC_UID_LIMIT + 1), 'one over the length bound'),
    ('S' * 512, 'far over the length bound'),
    ('<script>', 'markup'),
    (777, 'a non-string'),
]


class TestTheEchoedIdsAreStillBounded:
    """`UserPostsRequest.user_id` / `sec_uid`: optional, echo-only, and bounded
    anyway. Every rejection must land BEFORE a signed request — the request is
    refused, not forwarded and then filtered."""

    def test_the_declared_ceilings_are_the_ones_the_cases_below_assume(self):
        # The anchor that makes every length case above able to fail. Without
        # it a widened ceiling silently widens the tests with it.
        assert MAX_USER_ID_CHARS == USER_ID_LIMIT
        assert MAX_SEC_UID_CHARS == SEC_UID_LIMIT

    @pytest.mark.parametrize('user_id,label', BAD_USER_IDS)
    def test_a_malformed_user_id_is_422_before_signing(self, tmp_path, transport,
                                                       user_id, label):
        status, body = _posts(_app(tmp_path),
                              {'username': HANDLE, 'user_id': user_id})
        assert status == 422, label
        assert transport.calls == [], label
        # The rejection names the field, so a client can fix the right one.
        assert any(item['loc'][-1] == 'user_id' for item in body['detail']), (
            label, body['detail'])

    @pytest.mark.parametrize('sec_uid,label', BAD_SEC_UIDS)
    def test_a_malformed_sec_uid_is_422_before_signing(self, tmp_path, transport,
                                                       sec_uid, label):
        status, body = _posts(_app(tmp_path),
                              {'username': HANDLE, 'sec_uid': sec_uid})
        assert status == 422, label
        assert transport.calls == [], label
        assert any(item['loc'][-1] == 'sec_uid' for item in body['detail']), (
            label, body['detail'])

    @pytest.mark.parametrize('ids', [
        {'user_id': GENUINE_UID},
        {'sec_uid': GENUINE_SEC},
        {'user_id': GENUINE_UID, 'sec_uid': GENUINE_SEC},
        {'user_id': '9' * USER_ID_LIMIT, 'sec_uid': 'S' * SEC_UID_LIMIT},
        {'user_id': '0', 'sec_uid': 'a-b_c'},
        {'user_id': None, 'sec_uid': None},
        {},
    ])
    def test_the_accepted_id_shapes_are_not_refused(self, tmp_path, transport, ids):
        # The other side of every bound: an explicit null, an omission, and
        # both values AT the length limit must all still be served. A bound
        # that rejects its own limit value is a bound off by one.
        _single_page(transport, [('1', GENUINE)])
        status, body = _posts(_app(tmp_path), {'username': HANDLE, **ids})
        assert status == 200, ids
        assert body['count'] == 1

    def test_a_bounded_id_is_echoed_back_verbatim(self, tmp_path, transport):
        # What the bound is guarding: the value goes into the response body
        # unchanged, so the charset check is the only thing standing between a
        # caller-supplied string and a field every client reads.
        _single_page(transport, [('1', GENUINE)])
        status, body = _posts(_app(tmp_path), {
            'username': HANDLE, 'user_id': PINNED_UID, 'sec_uid': PINNED_SEC})
        assert status == 200
        assert body['user_id'] == PINNED_UID
        assert body['sec_uid'] == PINNED_SEC

    @pytest.mark.parametrize('field,value', [
        ('user_id', PINNED_UID), ('sec_uid', PINNED_SEC)])
    def test_profile_ignores_the_ids_rather_than_bounding_them(
            self, tmp_path, transport, field, value):
        # `ProfileRequest` declares NEITHER id, and pydantic's default
        # `extra='ignore'` drops them — so a `/profile` call carrying a valid
        # handle plus a stray id is served, not 422'd. Pinned because the
        # asymmetry with `/user/posts` above is otherwise surprising: the ids
        # are refused on `/profile` only when they arrive WITHOUT a username
        # (that is the `username: missing` case, not an id rejection).
        transport.script(lambda call: user_search_reply([profile_node()]))
        status, body = _profile(_app(tmp_path),
                                {'username': HANDLE, field: value})
        assert status == 200
        assert body['user_id'] == GENUINE_UID


# ========================================== 20: the unmintable-token 502 net
class TestAnUnmintableTokenIs502OnBothEndpoints:
    """`paging.encode` RAISES rather than trims when a cursor or the wire form
    is out of bounds. The net lives in the shared `_mint_token`, so `/search`
    is covered too — it previously escaped `run_in_executor` as a 500.

    502 and not 422: the caller's input was valid and the page was ALREADY
    served, so it is the upstream cursor that is odd. And not swallowed into a
    200 with a null token, which would hand back an unresumable page and hide a
    producer bug in `encode`."""

    def _break_encode(self, monkeypatch) -> None:
        def boom(*a, **k):
            raise ValueError('cursor out of bounds')

        monkeypatch.setattr(app_module, 'encode', boom)

    def test_search_returns_502(self, tmp_path, transport, monkeypatch):
        transport.script(_blocks)
        self._break_encode(monkeypatch)
        status, body = _post(_app(tmp_path), '/search',
                             {'type': 'keyword', 'query': 'ocean', 'limit': 10,
                              'fan_out': 1})[0]
        assert status == 502
        assert body['detail'] == app_module.UNMINTABLE_TOKEN_DETAIL

    def test_user_posts_returns_502(self, tmp_path, transport, monkeypatch):
        _pages(transport, authored_reply([('1', GENUINE)], cursor=10, has_more=True))
        self._break_encode(monkeypatch)
        status, body = _posts(_app(tmp_path), {'username': HANDLE, 'limit': 1})
        assert status == 502
        assert body['detail'] == app_module.UNMINTABLE_TOKEN_DETAIL

    def test_a_terminal_page_never_reaches_encode_at_all(self, tmp_path,
                                                          transport, monkeypatch):
        # `has_more=False` must short-circuit BEFORE the encoder, so a finished
        # stream cannot be turned into a 502 by the net above. The stub asserts
        # by being called: if it ever runs, the test fails.
        calls: list[int] = []

        def tripwire(*a, **k):
            calls.append(1)
            raise AssertionError('encode called for a has_more=False page')

        monkeypatch.setattr(app_module, 'encode', tripwire)
        _single_page(transport, [('1', GENUINE)])
        posts_status, posts_body = _posts(_app(tmp_path), {'username': HANDLE})
        assert posts_status == 200
        assert posts_body['page_token'] is None

        transport.script(lambda call: reply(range(1, 4), cursor=3, has_more=False))
        search_status, search_body = _post(
            _app(tmp_path), '/search',
            {'type': 'keyword', 'query': 'ocean', 'limit': 10, 'fan_out': 1})[0]
        assert search_status == 200
        assert search_body['page_token'] is None
        assert calls == []
