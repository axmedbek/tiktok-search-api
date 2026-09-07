"""End-to-end wiring tests for POST /search with page_token.

These drive the REAL FastAPI app — request validation, `_to_query`, the pool,
the client, the domain→HTTP map and token minting — over a synthetic identities
file, with `requests.Session.get` as the only seam. The HTTP status codes are
asserted through `app.py`'s own map rather than a copy of it in the test: a
re-implemented map is exactly the hole that lets a status flip go unnoticed.

`starlette.testclient` needs `httpx`, which is not a project dependency, so the
app is driven directly as the ASGI callable it is (see conftest.AsgiClient).

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    AsgiClient,
    drive,
    empty_reply,
    identity,
    reply,
    write_config,
    write_identities,
)

from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.client import (  # noqa: E402
    NIL_EMPTY_SESSION,
    SEARCH_ITEM_PATH,
    SEARCH_VIDEO_PATH,
)
from tiktoksearch.filters import PublishTime, SearchFilters, SortType  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    device_handle,
    encode,
)

DEVICE_A = 'DEVA'
DEVICE_B = 'DEVB'
RISK_CONTROL_NIL = 'hit_shark'
BASE_QUERY = {'type': 'keyword', 'query': 'ocean'}


def _app(tmp_path: Path, *, devices=(DEVICE_A,), **config_over):
    ids_path = tmp_path / 'ids.json'
    write_identities(ids_path, [identity(d) for d in devices], stamp=1_700_000_100)
    config_path = tmp_path / 'config.yaml'
    write_config(config_path, **config_over)
    return app_module.create_app(str(config_path))


def _post(app, *payloads) -> list[tuple[int, dict]]:
    """POST each payload to /search inside ONE app lifespan (so page 2 sees the
    pool state, daily cap and device that page 1 left behind)."""
    async def sequence():
        async with AsgiClient(app) as client:
            return [await client.post('/search', payload) for payload in payloads]

    return drive(sequence())


def _one(app, payload) -> tuple[int, dict]:
    return _post(app, payload)[0]


def _chain(app, payload, *follow_ups) -> list[tuple[int, dict]]:
    """Drive a real continuation chain in ONE lifespan: each follow-up receives
    the previous response body and returns the next payload, so page 2 uses the
    token page 1 actually minted and meets the pool state page 1 left."""
    async def sequence():
        async with AsgiClient(app) as client:
            results = [await client.post('/search', payload)]
            for build in follow_ups:
                results.append(await client.post('/search', build(results[-1][1])))
            return results

    return drive(sequence())


def _with_token(payload: dict):
    return lambda body: {**payload, 'page_token': body['page_token']}


def _query_hash(kind: str = 'keyword', term: str = 'ocean',
                filters: SearchFilters | None = None) -> str:
    return app_module._query_hash(kind, term, filters or SearchFilters())


def _mint(*endpoints: EndpointState, device: str = DEVICE_A,
          query_hash: str | None = None) -> str:
    """A token this server instance would accept — minted with the process's own
    secret, exactly as `_next_page_token` does."""
    return encode(PageToken(version=TOKEN_VERSION,
                            query_hash=query_hash or _query_hash(),
                            device_handle=device_handle(device),
                            endpoints=endpoints or (_resumable_primary(),)))


def _resumable_primary(cursor: int = 30) -> EndpointState:
    return EndpointState(SEARCH_VIDEO_PATH, cursor, 'SIDP', True, True)


def _retired_secondary() -> EndpointState:
    return EndpointState(SEARCH_ITEM_PATH, 0, '', False, True)


def _blocks(call) -> dict:
    """A healthy stream: ten fresh ids per window, TikTok's cursor advancing."""
    offset = int(call['offset'] or 0)
    first = offset + 1
    return reply(range(first, first + 10), cursor=offset + 10, has_more=True)


def _ids(body: dict) -> list[str]:
    return [record['id'] for record in body['results']]


class TestTokenRequestValidation:
    def test_a_garbage_token_is_a_client_error_not_a_502(self, tmp_path, transport):
        status, body = _one(_app(tmp_path),
                            {**BASE_QUERY, 'page_token': 'not-a-token'})
        assert status == 422
        assert transport.calls == []

    def test_a_tampered_token_is_422(self, tmp_path, transport):
        raw = _mint()
        body_part, tag = raw.split('.')
        tampered = body_part + '.' + ('A' if tag[0] != 'A' else 'B') + tag[1:]
        status, _ = _one(_app(tmp_path), {**BASE_QUERY, 'page_token': tampered})
        assert status == 422
        assert transport.calls == []

    def test_a_token_together_with_a_non_zero_cursor_is_422(self, tmp_path, transport):
        status, body = _one(_app(tmp_path),
                            {**BASE_QUERY, 'page_token': _mint(), 'cursor': 10})
        assert status == 422
        assert 'cursor' in body['detail']
        assert transport.calls == []

    def test_an_explicit_fan_out_above_one_with_a_token_is_422(self, tmp_path, transport):
        status, body = _one(_app(tmp_path),
                            {**BASE_QUERY, 'page_token': _mint(), 'fan_out': 2})
        assert status == 422
        assert body['detail'] == app_module.FAN_OUT_TOKEN_CONFLICT_MSG
        assert transport.calls == []

    def test_a_token_replayed_against_a_different_term_is_422(self, tmp_path):
        status, _ = _one(_app(tmp_path),
                         {'type': 'keyword', 'query': 'volcano',
                          'page_token': _mint()})
        assert status == 422

    def test_a_token_replayed_against_a_different_type_is_422(self, tmp_path):
        status, _ = _one(_app(tmp_path),
                         {'type': 'hashtag', 'query': 'ocean', 'page_token': _mint()})
        assert status == 422

    def test_a_token_replayed_with_the_filters_dropped_is_422(self, tmp_path):
        filtered = _query_hash(filters=SearchFilters(sort_type=SortType.MOST_LIKED))
        status, _ = _one(_app(tmp_path),
                         {**BASE_QUERY, 'page_token': _mint(query_hash=filtered)})
        assert status == 422

    def test_a_token_replayed_with_a_changed_filter_is_422(self, tmp_path):
        minted_under = SearchFilters(sort_type=SortType.MOST_LIKED,
                                     publish_time=PublishTime.LAST_MONTH)
        token = _mint(query_hash=_query_hash(filters=minted_under))
        status, _ = _one(_app(tmp_path), {
            **BASE_QUERY, 'page_token': token,
            'filters': {'sort_type': '1', 'publish_time': '7'}})
        assert status == 422

    def test_a_token_is_accepted_with_the_same_filters_it_was_minted_under(
            self, tmp_path, transport):
        transport.script(_blocks)
        minted_under = SearchFilters(sort_type=SortType.MOST_LIKED,
                                     publish_time=PublishTime.LAST_MONTH)
        token = _mint(_resumable_primary(), _retired_secondary(),
                      query_hash=_query_hash(filters=minted_under))
        status, body = _one(_app(tmp_path), {
            **BASE_QUERY, 'page_token': token, 'limit': 10,
            'filters': {'sort_type': '1', 'publish_time': '30'}})
        assert status == 200
        assert body['count'] == 10


class TestFanOutInteraction:
    def test_the_server_default_fan_out_is_coerced_to_one_not_rejected(
            self, tmp_path, transport):
        # The caller asked for nothing here, so the server's own default must
        # not turn their token into a 422.
        transport.script(_blocks)
        app = _app(tmp_path, devices=(DEVICE_A, DEVICE_B), default_fan_out=6)
        status, body = _one(app, {**BASE_QUERY, 'limit': 10,
                                  'page_token': _mint(_resumable_primary(),
                                                      _retired_secondary())})
        assert status == 200
        assert '+' not in body['device']       # one device served it, not a merge
        assert body['count'] == 10

    def test_a_fanned_out_page_mints_no_token(self, tmp_path, transport):
        # A merged multi-device page cannot be continued: there is no single
        # session to resume, so the response must not advertise one.
        transport.script(_blocks)
        app = _app(tmp_path, devices=(DEVICE_A, DEVICE_B), default_fan_out=2)
        status, body = _one(app, {**BASE_QUERY, 'limit': 10})
        assert status == 200
        assert '+' in body['device']
        assert body['page_token'] is None


class TestPoolCodeToHttpStatus:
    def test_a_capped_pinned_device_is_429(self, tmp_path, transport):
        # Through app.py's real PoolCode map — CAP is the only code that is a
        # 429; every other pool refusal is a 503.
        transport.script(_blocks)
        page = {**BASE_QUERY, 'limit': 20}
        app = _app(tmp_path, daily_request_cap_per_device=1)
        (first_status, first), (second_status, _) = _chain(
            app, page, _with_token(page))
        assert first_status == 200
        assert first['page_token']
        assert second_status == 429

    def test_a_vanished_pinned_device_is_503_with_no_signed_request(
            self, tmp_path, transport):
        token = _mint(_resumable_primary(), device='DEVICE-THAT-LEFT-THE-POOL')
        status, _ = _one(_app(tmp_path), {**BASE_QUERY, 'page_token': token})
        assert status == 503
        assert transport.calls == []

    def test_sessionless_risk_control_is_still_502(self, tmp_path, transport):
        transport.script(lambda call: empty_reply(nil=RISK_CONTROL_NIL))
        status, _ = _one(_app(tmp_path), BASE_QUERY)
        assert status == 502


class TestContinuationResponseContract:
    def test_a_session_tail_is_a_200_with_no_records_and_no_token(
            self, tmp_path, transport):
        # The plan contract: 200 {count: 0, has_more: false, page_token: null}
        # in ONE signed request — never a 502, and never charged to health.
        transport.script(lambda call: empty_reply(nil=NIL_EMPTY_SESSION))
        token = _mint(_resumable_primary(), _retired_secondary())
        status, body = _one(_app(tmp_path), {**BASE_QUERY, 'page_token': token})
        assert status == 200
        assert body['count'] == 0
        assert body['has_more'] is False
        assert body['page_token'] is None
        assert body['next_cursor'] is None
        assert len(transport.calls) == 1

    def test_next_cursor_is_tiktoks_cursor_not_the_record_count(
            self, tmp_path, transport):
        transport.script(_blocks)
        status, body = _one(_app(tmp_path), {**BASE_QUERY, 'limit': 15})
        assert status == 200
        assert body['count'] == 15
        # Two windows were walked, so TikTok's cursor is 20 while the old
        # `start + len(records)` computation would have said 15.
        assert body['next_cursor'] == 20

    def test_a_chained_second_page_shares_no_ids_with_the_first(
            self, tmp_path, transport):
        transport.script(_blocks)
        page = {**BASE_QUERY, 'limit': 20}
        (first_status, first), (second_status, second) = _chain(
            _app(tmp_path), page, _with_token(page))
        assert (first_status, second_status) == (200, 200)
        assert first['page_token']
        assert set(_ids(first)).isdisjoint(_ids(second))
        assert transport.sids[-1] is not None      # the session was carried

    def test_the_same_request_without_the_token_repeats_the_first_page(
            self, tmp_path, transport):
        # The control for the test above: identical payloads, no token, so the
        # zero overlap there is the session's work and not the fixture's.
        transport.script(_blocks)
        page = {**BASE_QUERY, 'limit': 20}
        (_, first), (_, second) = _chain(_app(tmp_path), page, lambda body: page)
        assert set(_ids(first)) == set(_ids(second))
