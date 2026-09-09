from __future__ import annotations
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, replace
from typing import Callable, Optional
import requests
from .config import SIGNER_LOCAL, SIGNER_RAPID, ClientConfig
from .errors import NotFound, RateLimited, SoftError, TransportError
from .filters import SearchKind, SearchPage, SearchQuery
from .mapping import flatten_profile, flatten_user, flatten_video
from .paging import EndpointState, PageToken, SeenWindow, cursor_bound, sanitize_search_id
from .rapid_signer import RapidSigner
from .signing import MetasecSigner
logger = logging.getLogger('tiktoksearch.client')
SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'
SEARCH_ITEM_PATH = '/aweme/v1/search/item/'
SEARCH_USER_PATH = '/aweme/v1/discover/search/'
# One user's profile, and that user's own posts. Both key on `sec_user_id`:
# `user_id` alone answers empty. UNVERIFIED against a capture in this repo —
# see the plan's open item, settled by the first live call.
USER_PROFILE_PATH = '/aweme/v1/user/profile/other/'
USER_POSTS_PATH = '/aweme/v1/aweme/post/'
# The three SEARCH endpoint paths, and the allow-list a `/search` page_token is
# decoded against — narrower than ENDPOINT_PATHS on purpose: a search token has
# no business naming the profile or posts endpoint.
SEARCH_PATHS: frozenset[str] = frozenset((SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH, SEARCH_USER_PATH))
# The only endpoint paths this service will ever call. A page_token may name no
# other path — the value ends up in a SIGNED TikTok URL sent on a live warm
# identity, so it is whitelisted, not merely type-checked. Each endpoint family
# hands `paging.decode` the narrowest list that covers its own token; this is
# the whole set those lists are drawn from.
ENDPOINT_PATHS: frozenset[str] = SEARCH_PATHS | frozenset((USER_PROFILE_PATH, USER_POSTS_PATH))
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
# TikTok `status_code` values that mean "there is no such user". UNVERIFIED in
# this repo — no capture here carries one — so this is deliberately a single
# named constant that one live check can correct, rather than a literal buried
# in a branch.
#
# What the `status_code: 0` premise DOES support: risk-control answers HTTP 200
# with `status_code: 0` and an empty payload, never a non-zero status, so no
# value listed here can launder a hit_shark into a 404. The empty-payload
# classification below is the only thing that ever sees a shadow-block, and it
# is reached only on a zero/absent status.
#
# What it does NOT support, and the real trade: a wrong value here turns some
# OTHER upstream non-zero-status error into a 404 instead of a retried 502, AND
# it spends that reply's identity-health evidence. `pool.run` reports EVERY
# escaping SoftError as an empty — non-zero-status ones included — so a
# misclassified status is one fewer health signal on the profile/posts paths.
# That is a lost signal, not a hidden hit_shark.
#
# Applied to the profile/posts shapes only; the search shape keeps treating
# every non-zero status as a retried SoftError.
NO_SUCH_USER_STATUSES: frozenset[int] = frozenset((2053, 10202))
# Client-facing failure text. Terse, and carrying no upstream message, no ids
# and nothing about the identity that served the request.
NO_SUCH_USER_MSG = 'no such user'
UNUSABLE_PROFILE_MSG = 'the profile reply carried no usable user id'
UNUSABLE_IDS_MSG = 'the user-search reply carried no usable ids for this username'

@dataclass(frozen=True)
class PayloadShape:
    """How ONE kind of reply answers "did this carry a payload, and what does
    empty mean here?".

    This is a per-call input rather than a module constant because the answer
    is endpoint-specific and getting it wrong fails in both directions. The
    search rule (`any of data / search_item_list / user_list is non-empty, or
    has_more`) applied to a profile reply — which carries a `user` object, no
    item list and no `has_more` — classifies every SUCCESSFUL profile call as a
    shadow-block and 502s it. Applied to a posts reply it would 502 a private
    or zero-post account, whose `aweme_list` is legitimately present-but-empty.

    - `has_payload` — did this reply carry what this endpoint's payload is.
      False means "empty", which for a `_direct` request means risk-control.
    - `session_aware` — this endpoint is a paginated SEARCH session, so its
      emptiness is additionally judged by `search_nil_info` and `has_more`,
      and a continuation may end in an allow-listed tail. Only search is.
    - `not_found_statuses` — non-zero `status_code` values that mean "no such
      user" and must raise `NotFound` immediately instead of being retried
      into a `SoftError`."""
    name: str
    has_payload: Callable[[dict], bool]
    empty_reason: str
    session_aware: bool = False
    not_found_statuses: frozenset[int] = frozenset()

def _has_search_items(data: dict) -> bool:
    return any(data.get(key) for key in _ITEM_LIST_KEYS)

def _has_profile_user(data: dict) -> bool:
    # A `user` key present but empty carries no profile, so it is NOT a
    # payload: `mapping.flatten_profile` would return None for it, and a None
    # profile must surface as an error, never as a 200 with a null body.
    #
    # This rejects an EMPTY `user` and nothing else, which is all a shadow-block
    # test should do. A non-empty `user` that happens to carry no `uid` also
    # flattens to None, and is deliberately NOT covered here: the reply DID
    # carry a payload, so calling it an empty would charge a healthy identity
    # for a merely odd payload. That case is closed in `TikTokClient.profile`,
    # which raises TransportError rather than returning a null profile.
    user = data.get('user')
    return isinstance(user, dict) and bool(user)

def _has_posts_list(data: dict) -> bool:
    # KEY PRESENCE, not truthiness, and that is the whole point. `aweme_list:
    # []` is an ordinary empty page — a private account, an account with no
    # posts, or the end of the stream — and must return 200 with zero records.
    # `aweme_list` ABSENT is a reply that dropped the payload entirely, which
    # is risk-control. Truth-testing the list would collapse the two.
    return 'aweme_list' in data

# The existing search rule, unchanged: any item list non-empty, or a page that
# promises more. `session_aware` folds in `has_more` and the nil/tail handling.
SEARCH_PAYLOAD = PayloadShape(name='search', has_payload=_has_search_items, empty_reason='no items, has_more=false (shadow-block)', session_aware=True)
PROFILE_PAYLOAD = PayloadShape(name='profile', has_payload=_has_profile_user, empty_reason='no user object (shadow-block)', not_found_statuses=NO_SUCH_USER_STATUSES)
POSTS_PAYLOAD = PayloadShape(name='posts', has_payload=_has_posts_list, empty_reason='no aweme_list key (shadow-block)', not_found_statuses=NO_SUCH_USER_STATUSES)
# How many pages ONE request may spend on ONE endpoint, on top of the
# `ceil(limit / page_count)` it strictly needs. Slack, because a page of
# `page_count` raw items rarely yields `page_count` NEW records: the two video
# endpoints overlap heavily and the shared dedup window drops the repeats, so a
# budget of exactly the arithmetic minimum would systematically under-fill
# `limit`.
PAGE_BUDGET_SLACK_PAGES = 4
# Absolute backstop on one endpoint's inner loop, whatever `limit` asks for.
# Every iteration is a signed request against a live warm identity (and a paid
# one in `signer: rapid`), so a misbehaving upstream must not be able to spin
# here even if the cursor guard is somehow satisfied, and a caller must not be
# able to buy unbounded work by naming a huge `limit`. Sized to sit just above
# the largest budget the shipped profile can ask for — `max_results_per_search:
# 300` at `page_count=10` needs 30 pages plus slack — so on that profile this
# bound is a safety net, not the thing that stops a normal stream.
MAX_PAGES_PER_ENDPOINT = 40
# Fixed query params for the two new endpoints. UNVERIFIED, like the paths
# themselves: `source=0` (the profile grid, not a favourites/liked tab) and
# `address_book_access=0` (do not claim contact-book permission) are what the
# v46 app sends, and `count=20` is its profile-grid page size. Unlike search,
# this endpoint is not known to cut `has_more` short at 20 — that finding is
# specific to the merged search endpoints and does not transfer.
POSTS_SOURCE = '0'
POSTS_PAGE_COUNT = 20
ADDRESS_BOOK_ACCESS = '0'
# `max_cursor=0` asks for the newest posts. The endpoint has no offset
# semantics, so this is a start marker, not an offset.
POSTS_START_CURSOR = 0
# `source_term` on a posts record: which stream produced it. Posts are reached
# by id, so there is no search term to carry — the owner's public user_id (the
# same value the endpoint contract returns to the caller) is the honest answer.
POSTS_SOURCE_PREFIX = 'posts:'
# `source_term` on a record `resolve_user` inspects: which stream produced it.
# Named for the same reason POSTS_SOURCE_PREFIX is — the two are the module's
# only source-term prefixes and they are read together.
RESOLVE_SOURCE_PREFIX = 'user:'
# How many user-search hits `resolve_user` scans for its exact match. One page,
# never more: an exact `unique_id` is on the first page of a user search or it
# is not there at all, and every extra page is another signed request.
RESOLVE_USER_COUNT = 10
_ID_LO = 7000000000000000000
_ID_HI = 7499999999999999999

def _synth_id() -> str:
    return str(random.randint(_ID_LO, _ID_HI))

@dataclass(frozen=True, slots=True)
class PageEnd:
    """Where one endpoint stopped: TikTok's own cursor (authoritative — it
    diverges from start_cursor + len(records) whenever dedup drops an item) and
    the `search_id` session handle the next request must echo — `''` on an
    endpoint that is not a search session and has none (see `user_posts`)."""
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

def _page_budget(limit: int, page_count: int) -> int:
    """How many pages one request may spend on one endpoint.

    Proportional to what `limit` actually needs (`ceil(limit / page_count)`)
    plus `PAGE_BUDGET_SLACK_PAGES`, and never past `MAX_PAGES_PER_ENDPOINT`.
    Proportionality is the whole point: a fixed page count is a bound on WORK
    that pretends to be a bound on USEFULNESS, and it silently inverts the
    caller's request once `limit` exceeds what those pages can carry — a bigger
    `limit` then returns FEWER records than a smaller one."""
    needed = -(-limit // page_count)
    return min(needed + PAGE_BUDGET_SLACK_PAGES, MAX_PAGES_PER_ENDPOINT)


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

def _user_search_params(keyword: str, cursor: int, count: int) -> dict:
    """Query params for one page of the user-search endpoint. Shared by the
    `/search` user path and `resolve_user` so the resolve cannot drift into
    querying a differently-shaped user search than the one that is known to
    work."""
    return {'keyword': keyword, 'count': str(count), 'cursor': str(cursor), 'type': '1', 'search_source': 'normal_search'}


def _posts_seed(token: Optional[PageToken]) -> EndpointState:
    """Resume state for the posts endpoint: the token's entry when it has one,
    else an unopened stream at the newest post. There is no legacy bare
    `cursor` to honour here — `/user/posts` has only ever been reachable by
    page_token, and a caller-supplied ms-epoch offset is not something to
    invent an entry point for."""
    state = token.state_for(USER_POSTS_PATH) if token is not None else None
    if state is not None:
        return state
    return EndpointState(path=USER_POSTS_PATH, cursor=POSTS_START_CURSOR, search_id='', has_more=True, started=False)


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
        # The signer mode decides BOTH which signer signs and which request path
        # runs. These were one switch (`bool(config.rapidapi_key)`) before, which
        # is why the paid signer had a monopoly on the working direct path.
        self._mode = config.resolved_signer()
        self._direct = self._mode in (SIGNER_LOCAL, SIGNER_RAPID)
        dq = config.device_query or {}
        self.device_id = config.device_id or dq.get('device_id') or _synth_id()
        self.iid = config.iid or dq.get('iid') or _synth_id()
        self.proxy = config.proxy
        if self._mode == SIGNER_RAPID:
            self._signer = RapidSigner(config)
        elif self._mode == SIGNER_LOCAL:
            self._signer = signer or MetasecSigner.for_v46(config)
        else:
            self._signer = signer or MetasecSigner(config)
        # Lazily built, and only ever by _rapid_fallback.
        self._fallback: Optional[RapidSigner] = None
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
            return _user_search_params(query.term, offset, count)
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
        # This endpoint's cursor bound, selected BY PATH through the same
        # `paging.cursor_bound` that `encode`/`decode` and `_paginate_posts`
        # use — one decision, one spelling. Every search path resolves to
        # MAX_ENDPOINT_CURSOR, so this is exactly the bound applied before.
        bound = cursor_bound(path)
        # direct mode paginates deeper with count=10 (count=20 returns has_more=false early)
        page_count = 10 if self._direct else 20
        pages = 0
        budget = _page_budget(limit, page_count)
        while len(out) < limit:
            if pages >= budget:
                # "Enough work for THIS request", not "this endpoint is spent".
                # The two used to be one thing: a flat 12-page ceiling that
                # also wrote has_more=False, retiring an endpoint TikTok was
                # still willing to serve. That was defensible only while
                # `limit <= page_count * ceiling` — the reasoning was "12 pages
                # that failed to fill `limit` are re-serving what the stream
                # already holds", which is a statement about a full window and
                # is simply false once `limit` exceeds what 12 pages can carry.
                # Measured on the old flat ceiling: limit=60 returned 105
                # unique records over 2 requests where limit=30 chained to 267,
                # because the general endpoint was marked has_more=False at
                # cursor=120 while TikTok itself still said has_more=true, and
                # no page_token was minted at all. At limit=300 it would have
                # fired on the very first request.
                #
                # So the budget only ENDS THIS REQUEST. `has_more` keeps
                # TikTok's own value from the last successful page (and
                # `progress` already published it), so the endpoint stays
                # resumable and the minted page_token continues it. Retirement
                # is left to the three cases where it is actually true:
                # TikTok's own has_more=false, a cursor that does not advance,
                # and an allow-listed session tail.
                logger.info('page budget %d spent on %s at cursor %d (limit %d, kept %d) — pausing, endpoint stays resumable (has_more=%s)', budget, path, cursor, limit, len(out), has_more)
                break
            pages += 1
            prev_cursor = cursor
            params = self._common_params()
            params.update(build_params(cursor, page_count))
            # session chaining: echo the previous response's search_id (direct mode)
            carried_session = bool(self._direct and search_id)
            if carried_session:
                params['search_id'] = search_id
            data = self._get_signed(path, params, shape=SEARCH_PAYLOAD, has_session=carried_session)
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
            if next_cursor > bound:
                # Bounded on the way IN with the same rule decode() applies, for
                # the same reason as `search_id`: minting a cursor past this
                # bound produces a token whose very next request is 422'd. A
                # value this large is not an offset any more, so the endpoint is
                # not resumable either — keep the records this page already
                # produced and retire it. The kept cursor is clamped so the
                # retired state stays encodable even when the caller's legacy
                # `cursor` started out of bounds.
                logger.warning('cursor %d beyond the resumable bound %d on %s — retiring endpoint', next_cursor, bound, path)
                next_cursor, has_more = (min(cursor, bound), False)
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

    def _match_user_node(self, username: str) -> dict:
        """The RAW user-search node whose `unique_id` IS `username`, in exactly
        ONE signed request.

        Reuses the EXISTING user-search endpoint rather than adding a lookup
        path, and reads the node straight off that one reply instead of going
        through `_search_users`: that would keep paginating until it had filled
        a `limit`, and every extra page is another signed request and another
        daily-cap unit spent on an answer that is either on the first page or
        not there at all.

        The RAW node is returned rather than a flattened record because its two
        callers want different projections of it — `resolve_user` wants the two
        ids, `profile_from_search` wants the whole flattened profile — and
        flattening here would force one of them to work backwards from the
        other's shape.

        `username` arrives already normalised from the API boundary (no leading
        `@`, bounded length and charset) — validating caller input belongs
        there, not down here.

        The match is an EXACT, case-insensitive `unique_id` comparison. User
        search is FUZZY and happily returns neighbours, so taking the first (or
        the best-scoring) hit would silently serve a different account's
        profile and posts — a wrong answer presented as a right one, which is
        worse than no answer. No exact hit is `NotFound` (404), never a
        SoftError: the caller named a user that is not there and the warm
        identity did nothing wrong.

        MEASURED against live TikTok, not assumed: a nonsense username is
        answered with a POPULATED `user_list`, so a bad username really does
        reach the no-exact-match `NotFound` below and not the
        empty-`user_list` path. `{"query": "definitely-not-a-real-user-999999",
        "type": "user", "limit": 10}` returned HTTP 200 with 10 fuzzy
        neighbours (`notzd9`, `user12342069911`, `user999999l08`,
        `notarealuser9`, `jaco.999`) — not one an exact match, so all of them
        are filtered out by the comparison above.

        An EMPTY `user_list` is a different case and must STAY a `SoftError`
        (502) through SEARCH_PAYLOAD: an empty item list on a sessionless first
        page IS the `hit_shark` signature, and nothing in the payload tells it
        apart from a username that does not exist. Answering 404 there would
        launder a possible shadow-block into a definitive claim about the
        account, and would throw away the identity-health report that is the
        only way the pool learns its warm identity has gone cold.
        """
        wanted = username.strip().lower()
        params = self._common_params()
        params.update(_user_search_params(wanted, 0, RESOLVE_USER_COUNT))
        data = self._get_signed(SEARCH_USER_PATH, params, shape=SEARCH_PAYLOAD)
        for raw in data.get('user_list') or []:
            if not isinstance(raw, dict):
                continue
            node = raw.get('user_info') or raw
            if not isinstance(node, dict):
                continue
            # A node with NO `unique_id` is skipped rather than compared: it can
            # never be the account that was asked for, and comparing it would
            # make an (impossible here, but not structurally impossible) empty
            # `username` match the first handle-less node in the reply.
            unique_id = str(node.get('unique_id') or '')
            if not unique_id or unique_id.lower() != wanted:
                continue
            return node
        logger.info('no exact username match in the user-search reply (device=%s, path=%s, error=%s)', self.device_id, SEARCH_USER_PATH, NotFound.__name__)
        raise NotFound(NO_SUCH_USER_MSG)

    def resolve_user(self, username: str) -> tuple[str, str]:
        """`(user_id, sec_uid)` for `username`, in exactly ONE signed request.

        The reply, the exact-match rule and each miss's failure class all
        belong to `_match_user_node`; this only projects the two ids out of the
        node it returns.
        """
        node = self._match_user_node(username)
        # `_match_user_node` proved `unique_id` is a non-empty string, so
        # flatten_user cannot return None here; `or {}` states that for the
        # reader (and for the type checker) instead of asserting it.
        record = flatten_user(node, f'{RESOLVE_SOURCE_PREFIX}{username.strip().lower()}') or {}
        user_id, sec_uid = (record.get('user_id'), record.get('sec_uid'))
        if user_id and sec_uid:
            return (str(user_id), str(sec_uid))
        # `unique_id` is unique upstream, so this IS the account and there
        # is no later candidate to look at. Both ids are required (the
        # profile and posts endpoints key on `sec_user_id`), so a match
        # missing one is unusable. TransportError, for the same reason as
        # in `profile` below: the reply arrived and parsed, so this is an
        # odd payload, not a distrusted identity and not a missing user.
        logger.warning('exact username match carried unusable ids (device=%s, path=%s, error=%s)', self.device_id, SEARCH_USER_PATH, TransportError.__name__)
        raise TransportError(UNUSABLE_IDS_MSG)

    def profile_from_search(self, username: str) -> dict:
        """`username`'s flattened profile, in exactly ONE signed request.

        Read off the USER-SEARCH node, because `/aweme/v1/user/profile/other/`
        cannot serve this client as it signs it (measured: HTTP 404 from the
        search host, a zero-length 200 on every general API host). `profile()`
        below stays in the tree as the upgrade path for when a capture settles
        that param set; this is what `POST /profile` actually calls.

        The node carries every profile field except `signature` and
        `region_code`, which are structurally ABSENT from it and are therefore
        ALWAYS None on this path — measured null across 15 users from two
        independent searches, so this is a property of the node, not of a
        particular account. `api/schemas.py` documents both as interim-null so
        a caller is not left guessing.

        No second signed request and no second daily-cap unit: the resolve that
        `user_id` + `sec_uid` would have cost IS this call, and both ids come
        back in the response so a caller never pays for it twice.
        """
        node = self._match_user_node(username)
        record = flatten_profile(node)
        if record is None:
            # The node matched by `unique_id` but carries no `uid`, so it
            # flattens to nothing and returning it would be a 200 with a null
            # profile. TransportError for exactly the reason spelled out in
            # `profile` below: `pool.run_call` reports every escaping SoftError
            # as an empty, and a well-formed-but-odd payload must not be
            # charged against a healthy warm identity.
            logger.warning('user-search node carried no usable user id (device=%s, path=%s, error=%s)', self.device_id, SEARCH_USER_PATH, TransportError.__name__)
            raise TransportError(UNUSABLE_PROFILE_MSG)
        return record

    def profile(self, user_id: str, sec_uid: str) -> dict:
        """One user's flattened profile, in ONE signed request.

        Both ids are passed because the endpoint keys on `sec_user_id`;
        `user_id` alone answers empty. `address_book_access=0` is sent
        unconditionally — this service never has a contact book to offer.
        """
        params = self._common_params()
        params.update({'user_id': user_id, 'sec_user_id': sec_uid, 'address_book_access': ADDRESS_BOOK_ACCESS})
        data = self._get_signed(USER_PROFILE_PATH, params, shape=PROFILE_PAYLOAD)
        user = data.get('user')
        record = flatten_profile(user) if isinstance(user, dict) else None
        if record is None:
            # `PROFILE_PAYLOAD.has_payload` only proved `user` is a non-empty
            # dict. A non-empty `user` carrying no `uid` gets through it and
            # flattens to None, and returning that would be a 200 with a null
            # profile. (The `isinstance` guard covers the same hole on the cold
            # legacy path, where the shape check does not run at all.)
            #
            # TransportError, deliberately, and NOT SoftError: `pool.run` turns
            # every escaping SoftError into `report_empty`, so a well-formed
            # but odd payload would be charged against the warm identity's
            # health, and DEFAULT_STALE_AFTER of those would retire the only
            # usable device and 503 every caller — the misdiagnosis
            # `.claude/rules/learned-lessons.md` exists to forbid. The reply
            # arrived, parsed and carried a payload; what failed is that the
            # payload is unusable, which is what TransportError already means
            # in this module. Nor is it NotFound: TikTok did not say this user
            # is gone, and answering 404 would invent a fact.
            logger.warning('profile reply carried no usable user id (device=%s, path=%s, error=%s)', self.device_id, USER_PROFILE_PATH, TransportError.__name__)
            raise TransportError(UNUSABLE_PROFILE_MSG)
        return record

    def user_posts(self, *, user_id: str, sec_uid: str, limit: int, page_token: Optional[PageToken]=None) -> SearchPage:
        """One request's worth of a user's own posts, as a SearchPage of
        `flatten_video` records — the identical record shape `/search` serves.

        Resumable across requests through the same `EndpointState` /
        `PageToken` machinery as search, with `search_id=''`: this endpoint is
        not a search session, it pages on TikTok's own `max_cursor` alone, so
        there is no session handle to echo and nothing to carry there.
        """
        state = _posts_seed(page_token)
        seen = SeenWindow(page_token.seen if page_token is not None else ())
        out: list[dict] = []
        if state.has_more:
            end = self._paginate_posts(out=out, seen=seen, user_id=user_id, sec_uid=sec_uid, limit=limit, start_cursor=state.cursor)
        else:
            # Exhausted by an earlier request: re-querying the tail spends a
            # signed request (a PAID one under `signer: rapid`) to be told the
            # same thing. Unreachable today — app.py mints no token for a page
            # with has_more=false, and tokens are HMAC-bound so no caller can
            # synthesise one — but the invariant belongs where it could be
            # violated, not only where it currently is not.
            end = PageEnd(has_more=False, cursor=state.cursor, search_id='')
        return SearchPage(records=out, cursor=state.cursor, next_cursor=end.cursor if end.has_more else None, has_more=end.has_more, endpoints=(EndpointState(path=USER_POSTS_PATH, cursor=end.cursor, search_id='', has_more=end.has_more, started=True),), seen=seen.recent())

    def _paginate_posts(self, *, out: list[dict], seen: SeenWindow, user_id: str, sec_uid: str, limit: int, start_cursor: int) -> PageEnd:
        """Page the posts endpoint on `max_cursor`, appending unique records
        into `out`/`seen`.

        A DEDICATED loop rather than `_paginate_into`, because that function is
        search-shaped in three places and two of them are INVERTED here:

        * it reads TikTok's `cursor`; a posts reply answers with `max_cursor`;
        * a search cursor is a forward OFFSET that must strictly INCREASE, so
          `_paginate_into` clamps with `max(cursor, ...)` and stops when the
          cursor fails to advance. A posts `max_cursor` is a millisecond epoch
          walking BACKWARDS through the user's timeline: the clamp would throw
          page 2's older cursor away and the guard would then stop the stream
          at one page;
        * it bounds every cursor by its OWN path's `paging.cursor_bound`,
          which for a search path is 100_000 — a bound a ms-epoch cursor
          exceeds on the very FIRST page, so borrowing the search family's
          value here would retire the endpoint immediately. This loop applies
          the same selector to its own path instead:
          `cursor_bound(USER_POSTS_PATH)` is `paging.MAX_MS_EPOCH_CURSOR`,
          enforced here on the way in and by `encode`/`decode` on both sides
          of a page_token.

        It also hardcodes `shape=SEARCH_PAYLOAD` and echoes `search_id` /
        reads `log_pb.impr_id`, neither of which this endpoint has. Threading
        five more parameters through the function `/search` depends on, in
        order to invert its cursor contract, buys nothing this loop does not
        already give. What DOES generalise is reused unchanged: `_page_budget`
        / MAX_PAGES_PER_ENDPOINT, `SeenWindow`, `_as_cursor`, `flatten_video`,
        `PageEnd` and the published `EndpointState`.
        """
        cursor = start_cursor
        has_more = False
        pages = 0
        # Routed through `cursor_bound`, never `MAX_MS_EPOCH_CURSOR` directly:
        # the selector is the single place that decides which family a path
        # belongs to. If this path's spelling ever diverged from
        # `paging._MS_EPOCH_CURSOR_PATHS`, posts would clamp at the offset
        # bound and fire the "did not advance" warning on page 1 — loud and
        # fail-closed — rather than mint a cursor `encode` then refuses.
        bound = cursor_bound(USER_POSTS_PATH)
        budget = _page_budget(limit, POSTS_PAGE_COUNT)
        source_term = f'{POSTS_SOURCE_PREFIX}{user_id}'
        while len(out) < limit:
            if pages >= budget:
                # Ends THIS request only; `has_more` keeps TikTok's own value
                # from the last page, so the endpoint stays resumable and the
                # minted page_token continues it.
                logger.info('page budget %d spent on %s at max_cursor %d (limit %d, kept %d) — pausing, endpoint stays resumable (has_more=%s)', budget, USER_POSTS_PATH, cursor, limit, len(out), has_more)
                break
            pages += 1
            prev_cursor = cursor
            params = self._common_params()
            params.update({'source': POSTS_SOURCE, 'user_id': user_id, 'sec_user_id': sec_uid, 'max_cursor': str(cursor), 'count': str(POSTS_PAGE_COUNT)})
            data = self._get_signed(USER_POSTS_PATH, params, shape=POSTS_PAYLOAD)
            # `aweme_list` PRESENT but empty is an ordinary page (private
            # account, no posts, end of stream) — POSTS_PAYLOAD already
            # separated that from an absent key, which is risk-control and
            # never gets here.
            raw_items = data.get('aweme_list') or []
            next_cursor = _as_cursor(data.get('max_cursor'), 0)
            has_more = bool(data.get('has_more'))
            # Progress means STRICTLY OLDER *and* inside this path's bound.
            # A cursor that stood still, moved forward, or came back
            # 0/unusable would re-request the same window forever, each
            # iteration another signed request. One ABOVE the bound is not a
            # creation timestamp at all, and `encode` refuses to mint it — so
            # publishing it as this endpoint's resume state would turn a
            # request that had ALREADY served records into a 500 the moment a
            # handler mints its page_token. Both cases fall into the
            # `not advanced` branch below: WARNING, has_more=False, and
            # `cursor` keeps its last in-bounds value, so the caller gets a 200
            # with the records served and no continuation — the same
            # clamp-and-retire posture `_paginate_into` takes.
            advanced = (0 < next_cursor <= bound
                        and (prev_cursor == POSTS_START_CURSOR or next_cursor < prev_cursor))
            if advanced:
                cursor = next_cursor
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                record = flatten_video(raw, source_term)
                key = record.get('id') if record else None
                if not record or not key or not seen.add(key):
                    continue
                out.append(record)
                if len(out) >= limit:
                    break
            if not has_more:
                # TikTok's own end of stream. Checked before the guard below so
                # a normal last page (which answers max_cursor=0) is not logged
                # as an anomaly.
                break
            if not advanced:
                logger.warning('max_cursor did not advance (%d -> %d) on %s — stopping', prev_cursor, next_cursor, USER_POSTS_PATH)
                has_more = False
                break
            if not raw_items:
                # An empty page mid-stream. The cursor did advance, so the
                # endpoint stays resumable and the next request continues from
                # there rather than re-asking for this window.
                break
        return PageEnd(has_more=has_more, cursor=cursor, search_id='')

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

    def _rapid_fallback(self, exc: TransportError) -> RapidSigner:
        """The paid signer to re-sign ONE failed local signing call with.

        Reached from exactly one place: `_sign`'s `except TransportError`, i.e.
        a HARD signing failure — the local signer produced no headers at all
        (a malformed URL, a signature reply missing a header, a crypto helper
        blowing up). Nothing else may reach it, and that is the whole coverage.

        It does NOT cover a stale sign key. An MSSDK bump raises nothing: the
        local signer keeps producing a well-formed signature that risk-control
        answers with HTTP 200 and an empty `data[]`, which arrives as
        `SoftError` — the one class this fallback must refuse, because swapping
        signers for an empty answer to a VALID signature is precisely the
        misdiagnosis `.claude/rules/lessons/anti-block.md` forbids. A stale key
        is diagnosed by hand instead (every identity failing at once shortly
        after a TikTok release; confirm by flipping one profile to
        `signer: rapid`), not papered over here.

        With no `rapidapi_key` configured there is nothing to fall back to, so
        the failure is logged and re-raised rather than silently retried."""
        if not self._config.rapidapi_key:
            logger.error('local signer failed and no RapidAPI fallback is configured: %s', exc)
            raise exc
        if self._fallback is None:
            # Once per client: the local signer is still tried first on every
            # later request, so a recovered local path stops spending quota.
            logger.warning('local signer failed (%s) — falling back to the RapidAPI signer', exc)
            self._fallback = RapidSigner(self._config)
        return self._fallback

    def _sign(self, url: str) -> dict[str, str]:
        """Headers for one signed request, from whichever signer this client
        holds. The direct path passes `iid`; the cold legacy path does not."""
        if not self._direct:
            return self._signer.sign(url=url, device_id=self.device_id)
        try:
            return self._signer.sign(url=url, device_id=self.device_id, iid=self.iid)
        except TransportError as exc:
            if self._mode != SIGNER_LOCAL:
                # Already on RapidAPI: there is nowhere narrower to fall back to.
                raise
            return self._rapid_fallback(exc).sign(url=url, device_id=self.device_id, iid=self.iid)

    def _get_signed(self, path: str, params: dict, *, shape: PayloadShape, has_session: bool=False) -> dict:
        """Sign and perform one signed request.

        `shape` is the caller's answer to "what counts as a payload on this
        endpoint, and what does an empty one mean" — see `PayloadShape`. It is
        required, with no default, so that adding a call kind cannot silently
        inherit the search-shaped rule and 502 its own successful replies.

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
            headers = self._sign(url)
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
                if status_code in shape.not_found_statuses:
                    # No retries and no identity penalty: re-signing cannot
                    # make a deleted user exist, and the warm identity did
                    # nothing wrong. The message carries no upstream text —
                    # it reaches the HTTP client as a 404 body.
                    logger.info('upstream reports no such user (path=%s, status=%s)', path, status_code)
                    raise NotFound(NO_SUCH_USER_MSG)
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
                nil: Optional[str] = None
                soft_empty = not shape.has_payload(data)
                # `search_nil_info` and `has_more` are SEARCH-SESSION concepts,
                # so every use of them lives behind `session_aware`. A profile
                # reply carries neither, and reading `has_more` off it is what
                # made a good profile look like a shadow-block; a posts reply
                # does carry `has_more`, but its emptiness is already settled by
                # `aweme_list` key presence, so it needs no second opinion.
                if shape.session_aware:
                    nil = (data.get('search_nil_info') or {}).get('search_nil_item')
                    soft_empty = soft_empty and not bool(data.get('has_more'))
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
                if shape.session_aware and has_session and (nil in _TAIL_NILS or (soft_empty and not nil)):
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
                    reason = nil if nil else shape.empty_reason
                    last_err = SoftError(f'empty {shape.name} result ({reason})')
                    logger.warning('empty result (attempt %d, device=%s, path=%s): %s', attempt, self.device_id, path, reason)
                    time.sleep(0.5 * (attempt + 1))
                    continue
            return data
        if isinstance(last_err, (RateLimited, SoftError)):
            raise last_err
        raise TransportError(f'request failed after {cfg.retries + 1} attempts: {last_err}')
