from __future__ import annotations
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Callable, Optional
import requests
from .config import ClientConfig
from .errors import RateLimited, SoftError, TransportError
from .filters import SearchKind, SearchPage, SearchQuery
from .mapping import flatten_user, flatten_video
from .paging import MAX_ENDPOINT_CURSOR, EndpointState, PageToken, SeenWindow, sanitize_search_id
from .rapid_signer import RapidSigner
from .signing import MetasecSigner
logger = logging.getLogger('tiktoksearch.client')
SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'
SEARCH_ITEM_PATH = '/aweme/v1/search/item/'
SEARCH_USER_PATH = '/aweme/v1/discover/search/'
# The only endpoint paths this service will ever call. A page_token may name no
# other path — the value ends up in a SIGNED TikTok URL sent on a live warm
# identity, so it is whitelisted, not merely type-checked.
SEARCH_PATHS: frozenset[str] = frozenset((SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH, SEARCH_USER_PATH))
# Merged video search: the general "Top" endpoint plus the Videos-tab endpoint.
_VIDEO_ENDPOINTS = ((SEARCH_VIDEO_PATH, 'data'), (SEARCH_ITEM_PATH, 'search_item_list'))
# Every key a search reply can put its items under. Used only to answer "did
# this reply carry ANY records" for the shadow-block heuristic — `user_list`
# belongs here too, or a user search whose page says has_more=false is misread
# as a shadow-block and 502s while carrying users.
_ITEM_LIST_KEYS = ('data', 'search_item_list', 'user_list')
# TikTok's `search_nil_item` value for "you sent a cursor with no live session".
NIL_EMPTY_SESSION = 'empty_session'
# `search_nil_item` for "the federated backend had nothing left for this
# session" — the other shape a finished session answers with.
NIL_FEDERATION_EMPTY = 'federation_empty'
# The ONLY nil values that mean "this search session has nothing more to give".
# An ALLOW-LIST on purpose, never a deny-list of known-bad values: the literal
# risk-control nil ByteDance sends is not pinned down anywhere, and it changes.
# Anything not listed here is treated as risk-control and surfaces as a
# SoftError (502) — anti-block invariants (a) and (b). Adding a value here is
# a deliberate decision to stop counting that shape against identity health.
_TAIL_NILS: frozenset[str] = frozenset((NIL_EMPTY_SESSION, NIL_FEDERATION_EMPTY))
# Backstop on one endpoint's inner loop. Every iteration is a PAID signed
# request, so a misbehaving upstream must not be able to spin here even if the
# cursor guard is somehow satisfied.
MAX_PAGES_PER_ENDPOINT = 12
_ID_LO = 7000000000000000000
_ID_HI = 7499999999999999999

def _synth_id() -> str:
    return str(random.randint(_ID_LO, _ID_HI))

@dataclass(frozen=True, slots=True)
class PageEnd:
    """Where one endpoint stopped: TikTok's own cursor (authoritative — it
    diverges from start_cursor + len(records) whenever dedup drops an item) and
    the `search_id` session handle the next request must echo."""
    has_more: bool
    cursor: int
    search_id: str

@dataclass(slots=True)
class PageProgress:
    """Mutable last-good state of one endpoint's inner loop.

    `_paginate_into` appends into a SHARED `out`/`seen` and can raise AFTER
    some of its inner pages already succeeded — a transport hiccup on inner
    page 2+ of the primary (a 25s timeout x 3 attempts) is the realistic case.
    Without this holder the caller only sees the exception, so the endpoint
    still looks unstarted and gets written back at its seeded state: an
    endpoint that had served records and held a live cursor + search_id is
    silently amputated for the rest of the stream, and the token then drives
    only the weaker `search/item/`. `_paginate_into` therefore publishes here
    after every successful inner page."""
    cursor: int
    search_id: str
    started: bool = False
    has_more: bool = True

    def resume_state(self, path: str) -> EndpointState:
        """The last-good state as a resumable endpoint entry."""
        return EndpointState(path=path, cursor=self.cursor, search_id=self.search_id,
                             has_more=self.has_more, started=True)

def _as_cursor(value: object, fallback: int) -> int:
    """TikTok's own `cursor`, coerced to a non-negative int (it is occasionally
    a numeric string). Falls back to the computed offset when unusable — the
    page token contract requires an int."""
    if value is None or isinstance(value, bool):
        return fallback
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        return fallback
    return cursor if cursor >= 0 else fallback

def _prefer_failure(current: Optional[Exception], exc: Optional[Exception]) -> Optional[Exception]:
    """Which failure to re-raise once every endpoint has been attempted and
    NONE produced a record.

    A `SoftError` wins over a `TransportError` whatever order they arrived in.
    It is the more diagnostic class — TikTok answered, emptily — and it is the
    only one `pool.run` turns into identity-health evidence, so re-raising a
    transport hiccup in its place would make a genuine empty invisible to
    IdentityStore (anti-block invariant (b))."""
    if current is None or exc is None:
        return current or exc
    if isinstance(exc, SoftError) and not isinstance(current, SoftError):
        return exc
    return current


def _seed(query: SearchQuery, path: str) -> EndpointState:
    """Resume state for `path`: the page token's entry when there is one, else
    an unopened endpoint at the legacy bare `cursor` (kept working, sessionless,
    as before). `started=False` is what distinguishes "never queried" from
    "resume at cursor 0" — see paging.EndpointState."""
    token: Optional[PageToken] = query.page_token
    state = token.state_for(path) if token is not None else None
    if state is not None:
        return state
    return EndpointState(path=path, cursor=query.cursor, search_id='', has_more=True, started=False)

def _seen_window(query: SearchQuery) -> SeenWindow:
    """Dedup set for this request, SEEDED from the page token's fingerprint
    window. Seeding is what makes cross-page dedup real: `seen` is otherwise
    per-request, so an endpoint resuming its own deeper window (or opened for
    the first time mid-stream) would re-emit records an earlier page already
    served, and a caller that concatenates pages shows them twice."""
    token: Optional[PageToken] = query.page_token
    return SeenWindow(token.seen if token is not None else ())

class TikTokClient:

    def __init__(self, config: ClientConfig, *, signer: Optional[MetasecSigner]=None) -> None:
        self._config = config
        self._direct = bool(config.rapidapi_key)
        dq = config.device_query or {}
        self.device_id = config.device_id or dq.get('device_id') or _synth_id()
        self.iid = config.iid or dq.get('iid') or _synth_id()
        self.proxy = config.proxy
        if self._direct:
            self._signer = RapidSigner(config)
        else:
            self._signer = signer or MetasecSigner(config)
        self._session = requests.Session()
        if config.proxy:
            self._session.proxies = {'http': config.proxy, 'https': config.proxy}

    def search(self, query: SearchQuery) -> SearchPage:
        if query.kind is SearchKind.USER:
            return self._search_users(query)
        return self._search_videos(query)

    def _search_videos(self, query: SearchQuery) -> SearchPage:
        filter_params = query.filters.to_query_params()

        def build(offset: int, count: int) -> dict:
            params = {'keyword': query.keyword, 'count': str(count), 'offset': str(offset), 'search_source': 'normal_search'}
            params.update(filter_params)
            return params

        def unwrap(item: dict) -> Optional[dict]:
            return item.get('aweme_info') or item.get('aweme') or (item if item.get('aweme_id') else None)

        # Direct mode: the app fetches results from the general "Top" endpoint
        # (data[]) AND the Videos-tab endpoint (search_item_list[]). One endpoint
        # alone stops at ~30 (has_more=false); chaining both merges to more —
        # this is why the phone shows more than a single endpoint returns.
        if self._direct:
            return self._search_videos_merged(query, build, unwrap)

        state = _seed(query, SEARCH_VIDEO_PATH)
        return self._paginate(path=SEARCH_VIDEO_PATH, build_params=build, items_key='data', unwrap=unwrap, flatten=flatten_video, source_term=query.source_term, limit=query.limit, start_cursor=state.cursor, search_id=state.search_id, seen=_seen_window(query))

    def _search_videos_merged(self, query: SearchQuery, build: Callable[[int, int], dict], unwrap: Callable[[dict], Optional[dict]]) -> SearchPage:
        """The direct two-endpoint video path: drive each endpoint from its own
        resume state into one shared, token-seeded dedup window."""
        out: list[dict] = []
        seen = _seen_window(query)
        # Each endpoint carries its own explicit state (cursor, search_id,
        # has_more, started) so a later request knows whether it is exhausted,
        # resumable, or not yet opened.
        ends: dict[str, EndpointState] = {}
        failure: Exception | None = None
        page_start = _seed(query, SEARCH_VIDEO_PATH).cursor
        for path, items_key in _VIDEO_ENDPOINTS:
            state = _seed(query, path)
            ends[path] = state
            if not state.has_more:
                # Exhausted: re-querying returns the tail empty, which the
                # sessionless heuristic would misread as risk-control.
                continue
            if len(out) >= query.limit:
                # Budget spent by the previous endpoint. This one keeps its
                # seeded state and stays resumable — the next request opens it
                # against a `seen` window seeded from the token, so its
                # overlapping first pages are deduped, not re-served.
                continue
            ends[path], exc = self._drive_endpoint(
                query=query, path=path, items_key=items_key, build=build,
                unwrap=unwrap, out=out, seen=seen, state=state)
            failure = _prefer_failure(failure, exc)
        has_more = any(s.has_more for s in ends.values())
        if not out and failure is not None:
            # Post-loop form of "raise only if NO endpoint produced records":
            # every resumable endpoint has been attempted by now, so an empty
            # `out` plus a real failure is the answer. Keeps the sessionless
            # page-1 hit_shark path at 502 and never returns a shadow-block as
            # a silent empty 200.
            raise failure
        primary = ends[SEARCH_VIDEO_PATH]
        return SearchPage(records=out, cursor=page_start, next_cursor=primary.cursor if has_more else None, has_more=has_more, endpoints=tuple(ends.values()), seen=seen.recent())

    def _drive_endpoint(self, *, query: SearchQuery, path: str, items_key: str, build: Callable[[int, int], dict], unwrap: Callable[[dict], Optional[dict]], out: list[dict], seen: SeenWindow, state: EndpointState) -> tuple[EndpointState, Optional[Exception]]:
        """Paginate one video endpoint from `state`. Returns its new end state
        plus whatever failure stopped it.

        A failure AFTER at least one inner page succeeded always yields a
        RESUMABLE state built from the published progress: the endpoint served
        records and holds a live cursor + search_id, and retiring it for a
        25s-timeout hiccup would silently hand the rest of the stream to the
        weaker endpoint. Before the first inner page succeeded there is nothing
        published, and the two failure classes mean different things:

        * `TransportError` — evidence about the network, not the endpoint. The
          seeded state is left exactly as resumable as it already was.
        * `SoftError` with nothing obtained — the endpoint answered emptily, so
          this is TikTok's own `has_more=false` wearing the sessionless
          shadow-block heuristic's clothes. Retiring it is the honest reading,
          and it stops every later "load more" from spending the retry budget
          on the same empty answer."""
        progress = PageProgress(cursor=state.cursor, search_id=state.search_id)
        try:
            end = self._paginate_into(out=out, seen=seen, path=path, build_params=build, items_key=items_key, unwrap=unwrap, flatten=flatten_video, source_term=query.source_term, limit=query.limit, start_cursor=state.cursor, search_id=state.search_id, progress=progress)
        except (SoftError, TransportError) as exc:
            if progress.started:
                logger.warning('endpoint %s failed after serving records (kept %d, still resumable at cursor %d): %s', path, len(out), progress.cursor, exc)
                return (progress.resume_state(path), exc)
            retired = isinstance(exc, SoftError)
            logger.warning('endpoint %s failed before any page (kept %d results, %s): %s', path, len(out), 'retired' if retired else 'still resumable', exc)
            return (replace(state, has_more=False) if retired else state, exc)
        return (EndpointState(path=path, cursor=end.cursor, search_id=end.search_id, has_more=end.has_more, started=True), None)

    def _search_users(self, query: SearchQuery) -> SearchPage:

        def build(offset: int, count: int) -> dict:
            return {'keyword': query.term, 'count': str(count), 'cursor': str(offset), 'type': '1', 'search_source': 'normal_search'}
        state = _seed(query, SEARCH_USER_PATH)
        return self._paginate(path=SEARCH_USER_PATH, build_params=build, items_key='user_list', unwrap=lambda item: item.get('user_info') or item, flatten=flatten_user, source_term=query.source_term, limit=query.limit, start_cursor=state.cursor, search_id=state.search_id, seen=_seen_window(query))

    def _paginate(self, *, path: str, build_params: Callable[[int, int], dict], items_key: str, unwrap: Callable[[dict], Optional[dict]], flatten: Callable[[dict, str], Optional[dict]], source_term: str, limit: int, start_cursor: int, seen: SeenWindow, search_id: str='') -> SearchPage:
        """Single-endpoint pagination (user search, and the legacy cold path)."""
        out: list[dict] = []
        end = self._paginate_into(out=out, seen=seen, path=path, build_params=build_params, items_key=items_key, unwrap=unwrap, flatten=flatten, source_term=source_term, limit=limit, start_cursor=start_cursor, search_id=search_id)
        return SearchPage(records=out, cursor=start_cursor, next_cursor=end.cursor if end.has_more else None, has_more=end.has_more, endpoints=(EndpointState(path=path, cursor=end.cursor, search_id=end.search_id, has_more=end.has_more, started=True),), seen=seen.recent())

    def _paginate_into(self, *, out: list[dict], seen: SeenWindow, path: str, build_params: Callable[[int, int], dict], items_key: str, unwrap: Callable[[dict], Optional[dict]], flatten: Callable[[dict, str], Optional[dict]], source_term: str, limit: int, start_cursor: int, search_id: str='', progress: Optional[PageProgress]=None) -> PageEnd:
        """Paginate one endpoint, appending unique records into `out`/`seen`.
        Returns the end state (has_more, TikTok's cursor, search_id) so a caller
        can resume this endpoint in a LATER request — a non-zero offset without
        the session's search_id gets `empty_session` from TikTok. Shared by
        _paginate and the direct multi-endpoint path so both endpoints dedupe
        against the same window.

        `progress`, when given, receives the last-good cursor/search_id after
        EVERY successful inner page. `out`/`seen` are mutated in place, so a
        raise from a later page still leaves records served; publishing the
        matching resume state is what stops that partial success from looking
        like "never started" to the caller."""
        cursor = start_cursor
        has_more = False
        # direct mode paginates deeper with count=10 (count=20 returns has_more=false early)
        page_count = 10 if self._direct else 20
        pages = 0
        while len(out) < limit:
            if pages >= MAX_PAGES_PER_ENDPOINT:
                # An endpoint that burned this many PAID signs without
                # filling `limit` is retired rather than left resumable, and
                # the cost of that is real: retiring it can end the whole
                # stream. Measured — 21 records against limit=60, the general
                # endpoint marked has_more=False at cursor=120 while TikTok
                # itself still said has_more=true, and no page_token minted at
                # all. Accepted because reaching the ceiling takes 12 pages at
                # a ~90% duplicate rate against a 12-page dedup window, i.e. an
                # endpoint that is re-serving what the stream already holds.
                logger.warning('page ceiling %d reached on %s — stopping', MAX_PAGES_PER_ENDPOINT, path)
                has_more = False
                if progress is not None:
                    progress.has_more = False
                break
            pages += 1
            prev_cursor = cursor
            params = self._common_params()
            params.update(build_params(cursor, page_count))
            # session chaining: echo the previous response's search_id (direct mode)
            carried_session = bool(self._direct and search_id)
            if carried_session:
                params['search_id'] = search_id
            data = self._get_signed(path, params, has_session=carried_session)
            raw_items = data.get(items_key) or []
            # A stream's cursor NEVER moves backwards. TikTok answers a
            # finished session with `cursor: 0` (an ended session has no
            # offset), which would rewind the stored cursor 30 -> 0 and make
            # the next continuation re-request a window this stream already
            # served — paid signs spent on records the dedup window then drops.
            # Clamping also leaves the non-advancing guard below intact: it
            # fires on `<= prev_cursor`, and the clamp yields exactly
            # `prev_cursor` in precisely the cases where the cursor failed to
            # advance.
            next_cursor = max(cursor, _as_cursor(data.get('cursor'), cursor + len(raw_items)))
            has_more = bool(data.get('has_more'))
            if next_cursor > MAX_ENDPOINT_CURSOR:
                # Bounded on the way IN with the same rule decode() applies, for
                # the same reason as `search_id`: minting a cursor past this
                # bound produces a token whose very next request is 422'd. A
                # value this large is not an offset any more, so the endpoint is
                # not resumable either — keep the records this page already
                # produced and retire it. The kept cursor is clamped so the
                # retired state stays encodable even when the caller's legacy
                # `cursor` started out of bounds.
                logger.warning('cursor %d beyond the resumable bound %d on %s — retiring endpoint', next_cursor, MAX_ENDPOINT_CURSOR, path)
                next_cursor, has_more = (min(cursor, MAX_ENDPOINT_CURSOR), False)
            # Bounded on the way IN, with the same rules decode() applies, so a
            # minted token can never be one the next request would 422. An
            # unusable impr_id keeps the session we already had.
            search_id = sanitize_search_id(
                (data.get('log_pb') or {}).get('impr_id')
                or (data.get('extra') or {}).get('logid')) or search_id
            for raw in raw_items:
                node = unwrap(raw)
                if node is None:
                    continue
                record = flatten(node, source_term)
                key = record and (record.get('id') or record.get('username'))
                if not record or not key or not seen.add(key):
                    continue
                out.append(record)
                if len(out) >= limit:
                    break
            cursor = next_cursor
            if progress is not None:
                # This inner page SUCCEEDED: publish before anything can raise.
                progress.started = True
                progress.cursor = cursor
                progress.search_id = search_id
                progress.has_more = has_more
            if not has_more or not raw_items:
                break
            # TikTok's cursor is authoritative; if it does not ADVANCE we would
            # re-request the same window forever, each iteration a paid sign.
            # Compare against the previous iteration, not the initial cursor.
            if cursor <= prev_cursor:
                logger.warning('cursor did not advance (%d) on %s — stopping', cursor, path)
                has_more = False
                if progress is not None:
                    progress.has_more = False
                break
        return PageEnd(has_more=has_more, cursor=cursor, search_id=search_id)

    def _common_params(self) -> dict:
        cfg = self._config
        if self._direct:
            # full warm device fingerprint + fresh per-request timestamps
            now = int(time.time())
            params = dict(cfg.device_query or {})
            params.setdefault('device_id', self.device_id)
            params.setdefault('iid', self.iid)
            params.setdefault('aid', str(cfg.app_id))
            params['ts'] = str(now)
            params['_rticket'] = str(now * 1000)
            return params
        return {'aid': str(cfg.app_id), 'app_name': 'musical_ly', 'version_code': cfg.version_code, 'version_name': cfg.app_version, 'device_platform': 'android', 'device_type': cfg.device_type, 'os_version': cfg.os_version, 'ssmix': 'a', 'device_id': self.device_id, 'iid': self.iid, 'channel': cfg.channel}

    def _get_signed(self, path: str, params: dict, *, has_session: bool=False) -> dict:
        """Sign and perform one search request.

        `has_session` says the outgoing request CARRIES a `search_id`. That
        changes how an empty reply is classified: a sessionless first page that
        comes back empty is hit_shark risk-control (SoftError → 502), while a
        live session answering with an ALLOW-LISTED tail shape has merely
        reached its end and is returned as an ordinary empty page. A session
        does not launder an unrecognised nil: that is still risk-control — see
        the empty-result block below."""
        cfg = self._config
        last_err: Exception | None = None
        for attempt in range(cfg.retries + 1):
            if self._direct:
                host = cfg.search_host
            else:
                host = cfg.api_hosts[attempt % len(cfg.api_hosts)]
            url = host + path + '?' + urllib.parse.urlencode(params)
            if self._direct:
                headers = self._signer.sign(url=url, device_id=self.device_id, iid=self.iid)
            else:
                headers = self._signer.sign(url=url, device_id=self.device_id)
            try:
                resp = self._session.get(url, headers=headers, timeout=cfg.request_timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                logger.warning('request error (attempt %d): %s', attempt, exc)
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                raise RateLimited('TikTok rate-limited this request')
            if resp.status_code != 200 or not resp.content:
                last_err = TransportError(f'HTTP {resp.status_code} len {len(resp.content)}')
                logger.warning('bad response (attempt %d): %s', attempt, last_err)
                time.sleep(0.5 * (attempt + 1))
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                last_err = exc
                continue
            status_code = data.get('status_code') if isinstance(data, dict) else None
            if status_code not in (0, None):
                message = data.get('message') or data.get('status_msg') or 'unknown'
                last_err = SoftError(message)
                logger.warning('soft error (attempt %d): %s', attempt, message)
                time.sleep(0.5 * (attempt + 1))
                continue
            # direct mode: detect risk-control empty ("hit_shark") so callers see a
            # clear error instead of a silent 200 + empty results. TikTok signals a
            # soft-block several ways: an explicit search_nil_info, OR simply an empty
            # item list with has_more=false (no nil block at all). Both mean the
            # device/identity was shadow-rejected — retry (rotates timestamps) then fail.
            if self._direct and isinstance(data, dict):
                nil = (data.get('search_nil_info') or {}).get('search_nil_item')
                # Every item key, `user_list` included: a USER search whose page
                # legitimately answers has_more=false was otherwise read as
                # "no items, has_more=false" and 502'd while carrying users.
                items_any = any(data.get(key) for key in _ITEM_LIST_KEYS)
                soft_empty = (not items_any) and not bool(data.get('has_more'))
                # Session-shaped emptiness is NOT risk-control. A request that
                # carried a search_id and got an ALLOW-LISTED tail nil, or
                # nothing at all with has_more=false and no nil block, has
                # simply reached the end of (or outlived) that session. Raising
                # SoftError here would 502 the last "load more" of every search
                # AND count against identity health — three of those retire the
                # only warm identity (DEFAULT_STALE_AFTER) and 503 every caller.
                # Return it as an empty page, with no retries: re-signing cannot
                # revive a finished session.
                #
                # The nil test is an allow-list and fails CLOSED: an explicit
                # nil this code does not recognise (hit_shark, or whatever
                # replaces it) is risk-control even on a continuation, and must
                # fall through to the SoftError below. Judging it by emptiness
                # alone would hand the caller a silent 200 with zero records and
                # leave IdentityStore blind to a shadow-block, because pool.run
                # reports neither ok nor empty for an empty continuation.
                if has_session and (nil in _TAIL_NILS or (soft_empty and not nil)):
                    logger.info('search session ended (device=%s, path=%s, reason=%s)', self.device_id, path, nil or 'tail')
                    # A tail is TERMINAL for this endpoint, whatever `has_more`
                    # the reply carries. Classifying a reply as "this session
                    # has nothing more to give" and then letting the caller
                    # read `has_more: true` off the same reply publishes a
                    # RESUMABLE EndpointState, mints a page_token, and every
                    # follow-up spends another PAID sign on the same empty
                    # answer — a stream that never terminates. Normalising
                    # here, at the boundary that made the classification, is
                    # what keeps `paging.EndpointState`'s contract
                    # (`started=True, has_more=False` — exhausted, never
                    # re-query) true for the state that gets published.
                    return {**data, 'has_more': False}
                if nil or soft_empty:
                    reason = nil if nil else 'no items, has_more=false (shadow-block)'
                    last_err = SoftError(f'empty search result ({reason})')
                    logger.warning('empty result (attempt %d, device=%s, path=%s): %s', attempt, self.device_id, path, reason)
                    time.sleep(0.5 * (attempt + 1))
                    continue
            return data
        if isinstance(last_err, (RateLimited, SoftError)):
            raise last_err
        raise TransportError(f'request failed after {cfg.retries + 1} attempts: {last_err}')
