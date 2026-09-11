from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator
from ..filters import PublishTime, SearchKind, SortType
from ..limits import MAX_POSTS_LIMIT, MAX_QUERY_CHARS, MAX_SEARCH_LIMIT, MAX_USERNAME_CHARS, USERNAME_PATTERN
from ..paging import MAX_PAGE_TOKEN_CHARS

# Those five bounds moved to `..limits` so a non-HTTP caller (the broker
# worker) can reach one without importing the FastAPI application. They are
# still readable AS `api.schemas.MAX_QUERY_CHARS` etc., which is what `app.py`
# and the existing tests import. `MAX_SEARCH_LIMIT` is the new name for what
# `SearchRequest.limit` spelled as a bare `le=300`; `MAX_POSTS_LIMIT` derives
# from it there instead of restating the number.

class FiltersIn(BaseModel):
    sort_type: Optional[SortType] = Field(default=None, description='Result ordering. `0` = relevance (default), `1` = most liked.')
    publish_time: Optional[PublishTime] = Field(default=None, description='Recency window. `0` = all time, `1` = last 24h, `7` = last week, `30` = last month, `90` = last 3 months, `180` = last 6 months.')
    model_config = {'json_schema_extra': {'examples': [{'sort_type': '1', 'publish_time': '30'}]}}

class SearchRequest(BaseModel):
    type: SearchKind = Field(description='What to search: keyword, hashtag, or user.')
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS, description='The search term.')
    limit: int = Field(default=30, ge=1, le=MAX_SEARCH_LIMIT, description='Max results per page (server-capped by `max_results_per_search`, 300 on the shipped profile). Cost scales with it: one page of results costs one signed request per ~10 raw items per endpoint, so a large `limit` buys depth in a single call at the price of latency. It spends one daily-cap unit either way, so `limit=300` in one call and `limit=30` chained are priced identically against the cap.')
    cursor: int = Field(default=0, ge=0, description='Legacy pagination offset. Kept for compatibility, but a bare offset carries no TikTok search session and comes back empty (`empty_session`) — use `page_token` instead. Start at 0.')
    page_token: Optional[str] = Field(default=None, max_length=MAX_PAGE_TOKEN_CHARS, description="Opaque continuation token from a prior response's `page_token`. It carries TikTok's own cursor plus the `search_id` search session, so the next page actually returns fresh results. It is authenticated with a per-process secret and is only valid for the same type/query/filters on the server instance that minted it: tokens do NOT survive a server restart (a TikTok search session is short-lived anyway), and a tampered or stale one is rejected with 422. It pins the device that served the previous page, so the server default `fan_out` is coerced to 1 — an explicit `fan_out` above 1 alongside a token is rejected with 422. Mutually exclusive with a non-zero `cursor`.")
    fan_out: Optional[int] = Field(default=None, ge=1, le=32, description='Query this many devices in parallel and merge+dedupe their results. Higher = more unique results per call (each device returns a shallow window), at the cost of one daily-cap unit per device. Capped at the pool size. Defaults to the server `default_fan_out` (so a plain request already returns a merged page). Ignored (forced to 1) when `page_token` is supplied, because a search session lives on a single device.')
    filters: Optional[FiltersIn] = Field(default=None, description='Optional filters (video searches only).')
    model_config = {'json_schema_extra': {'examples': [{'type': 'keyword', 'query': 'climate change', 'limit': 50, 'fan_out': 8}, {'type': 'user', 'query': 'nasa', 'limit': 10}]}}

class SearchResponse(BaseModel):
    query: str
    type: SearchKind
    device: str = Field(description='Label of the device that served the request.')
    count: int
    cursor: int = Field(description='The cursor this page started from.')
    next_cursor: Optional[int] = Field(default=None, description="TikTok's own cursor at the end of this page. Informational — pass `page_token` back, not this, to fetch the next page. Null when there are no more results.")
    page_token: Optional[str] = Field(default=None, description='Pass this back as `page_token` to fetch the next page. Null when there are no more results (or when the page cannot be resumed, e.g. a merged multi-device page).')
    has_more: bool = Field(description='Whether more results are available beyond this page.')
    elapsed_s: float
    results: list[dict[str, Any]]

class DeviceStatus(BaseModel):
    label: str
    device_id: str
    iid: str
    proxy: Optional[str] = None
    used_today: int
    daily_cap: int
    remaining_today: int
    busy: bool

class HealthResponse(BaseModel):
    status: str
    device_count: int
    idle: int
    total_daily_capacity: int
    capacity_remaining_today: int
    devices: list[DeviceStatus]

# --- user profile + user posts -------------------------------------------
# Both endpoints are entered by HANDLE, bounded and charset-checked HERE, at
# the boundary (.claude/rules/security.md), never downstream — with
# `MAX_USERNAME_CHARS` / `USERNAME_PATTERN` imported from `..limits`, because
# the broker worker enters the same two endpoints and must apply the same rule.
# `user_id` is a numeric string upstream and `sec_uid` a base64url-ish blob.
# Neither is used to build a request on this interim path — they are echoed
# into the posts response — but both are bounded anyway: an unbounded string a
# caller can put in a response body is a channel, and the day one of them DOES
# reach a signed URL again (see `client.profile`, the upgrade path) the bound is
# already where it belongs.
MAX_USER_ID_CHARS = 32
USER_ID_PATTERN = r'^[0-9]+$'
MAX_SEC_UID_CHARS = 200
SEC_UID_PATTERN = r'^[A-Za-z0-9_-]+$'
# Where each payload came from, as a machine-readable field rather than a
# footnote in a docstring. `user_search` is the user-search node the profile is
# flattened from; `search` is the keyword-search reply the posts are filtered
# out of. A caller that later sees `profile` / `posts` here is talking to a
# server that reached the real upstream endpoints.
PROFILE_SOURCE = 'user_search'
POSTS_SOURCE = 'search'
# `/user/posts` never returns an account's full post history on this path — see
# `UserPostsResponse.complete`.
POSTS_COMPLETE = False
# `/user/posts` answers "what did this account post RECENTLY", and that is the
# whole point of the endpoint: it is not "everything this account ever posted".
# 30 days is the default because that is where the measured yield is. On
# `@bakuesaz` (5236 posts) the unfiltered relevance search returned 18 posts
# spread over 17 MONTHS — the same order of magnitude of records as one 30-day
# window (17), except almost none of them recent. Narrowing the window does not
# cost records here; it changes which records they are.
DEFAULT_POSTS_PERIOD = PublishTime.LAST_MONTH
# `PublishTime.ALL_TIME` ('0') is not a recency window, it is the ABSENCE of
# one — i.e. exactly the unfiltered behaviour this endpoint moved away from.
# Rejected at the boundary rather than quietly honoured, so a caller asking for
# "everything ever" is told this endpoint does not serve it (`/search` on the
# handle does, unfiltered, with the 17-month spread that implies).
POSTS_PERIOD_ALL_TIME_MSG = (
    'period must be a recency window — 1, 7, 30, 90 or 180 days. 0 (all time) is not one: '
    '/user/posts returns RECENT posts. Use /search for an unfiltered search on the handle.'
)


class _HandleRequest(BaseModel):
    """Shared, validated `username` field.

    A leading `@` is stripped (exactly one — `@@bob` is a typo, not a handle,
    and is rejected rather than guessed at) and surrounding whitespace is
    removed BEFORE the length and charset checks, so `"@bakuesaz"`,
    `" bakuesaz "` and `"bakuesaz"` are one request. An empty or
    whitespace-only value fails `min_length` and is a 422: it must never reach
    `client._match_user_node`, where an empty handle is a handle no node can
    match rather than an error."""
    username: str = Field(min_length=1, max_length=MAX_USERNAME_CHARS, pattern=USERNAME_PATTERN, description="TikTok handle, with or without a leading `@` (e.g. `@bakuesaz`). Letters, digits, `.` and `_`, up to 24 characters. This is the ONLY way to enter either endpoint on the current path: both are served from a search reply keyed on the handle, so there is no id-only entry point — a request carrying just `user_id`/`sec_uid` is rejected with 422.")

    @field_validator('username', mode='before')
    @classmethod
    def _normalise_username(cls, value: Any) -> Any:
        # `mode='before'` so the normalisation happens FIRST and the declared
        # length/charset bounds then judge the value that will actually be
        # used. A non-string is handed back untouched for pydantic to report
        # as the type error it is.
        if not isinstance(value, str):
            return value
        return value.strip().removeprefix('@')


class ProfileRequest(_HandleRequest):
    model_config = {'json_schema_extra': {'examples': [{'username': '@bakuesaz'}]}}


class ProfileResponse(BaseModel):
    username: Optional[str] = Field(description='The handle as TikTok spells it.')
    user_id: str = Field(description="TikTok's numeric user id. Pass it (with `sec_uid`) to `/user/posts` to keep the ids alongside a filtered page.")
    sec_uid: Optional[str] = Field(default=None, description="TikTok's `sec_uid` for this account. Returned on purpose: the real posts endpoint keys on it, so a caller that stores it now needs no second lookup later.")
    display_name: Optional[str] = None
    signature: Optional[str] = Field(default=None, description='Profile bio. **Always null on this path**: the profile is flattened from the user-search node, and that node carries no `signature` at all (measured null across 15 users from two independent searches). It is not "this account has no bio".')
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    aweme_count: Optional[int] = Field(default=None, description='Number of posts TikTok reports for the account. Not the number `/user/posts` can return — see `UserPostsResponse.complete`.')
    heart_count: Optional[int] = Field(default=None, description='Total likes RECEIVED across the account (TikTok `total_favorited`), not likes it gave.')
    region_code: Optional[str] = Field(default=None, description='Two-letter region. **Always null on this path**, for the same structural reason as `signature` — the user-search node does not carry it.')
    verified: bool = False
    private: bool = False
    avatar_url: Optional[str] = Field(default=None, description="Avatar image URL on TikTok's own CDN, passed through unproxied.")
    # No default, so it lands in the generated schema's `required` list — see
    # `UserPostsResponse.source`. The handler passes PROFILE_SOURCE.
    source: str = Field(description='Which upstream reply this profile was read from. `user_search` means the user-search node, the only source that serves it today.')
    device: str = Field(description='Label of the device that served the request.')
    elapsed_s: float


class UserPostsRequest(_HandleRequest):
    user_id: Optional[str] = Field(default=None, min_length=1, max_length=MAX_USER_ID_CHARS, pattern=USER_ID_PATTERN, description='Optional. The `user_id` a prior `/profile` call returned. It is not needed to fetch anything — this path searches on the handle — but supplying it pins the ids in the response even on a page whose records were all filtered out.')
    sec_uid: Optional[str] = Field(default=None, min_length=1, max_length=MAX_SEC_UID_CHARS, pattern=SEC_UID_PATTERN, description='Optional, and exactly like `user_id`: echoed back, never used to fetch.')
    limit: int = Field(default=30, ge=1, le=MAX_POSTS_LIMIT, description='Max results to ASK SEARCH for, PER KEYWORD (server-capped by `max_results_per_search`), and also the ceiling on `count`: records by other authors are dropped and the surviving records from every keyword are unioned, then trimmed back to this many, OLDEST first out — so `count` never exceeds it and what a trim discards is the least recent. `count` is normally well below it anyway — see `count`.')
    period: PublishTime = Field(default=DEFAULT_POSTS_PERIOD, description='Recency window to search, in days: `1`, `7`, `30` (default), `90` or `180`. THIS IS THE POINT OF THE ENDPOINT — it answers "what did this account post recently", not "everything this account ever posted". It is the same `publish_time` filter `/search` accepts, so `0` (all time) is REJECTED with 422 here: an unfiltered relevance search on a handle spreads a handful of posts across many months (measured: 18 posts over 17 months for a 5236-post account), which is what this window exists to avoid. A narrower window is not cheaper and a wider one is not more complete — cost is fixed per keyword (see `cap_units`) and the window usually exhausts inside a single request.')
    page_token: Optional[str] = Field(default=None, max_length=MAX_PAGE_TOKEN_CHARS, description="Opaque continuation token from a prior response's `page_token`. Bound to this endpoint, this handle AND this `period`: a `/search` token is rejected here with 422, so is this one at `/search`, and so is one minted under a different `period`. A continuation searches the HANDLE ALONE (no display-name keyword, no resolve), because that is the single query the token was minted for — so it also costs one cap unit instead of the first page's several. It pins the device that served the previous page, and like every page_token it does not survive a server restart.")
    model_config = {'json_schema_extra': {'examples': [{'username': '@bakuesaz', 'limit': 60}, {'username': '@bakuesaz', 'period': '7'}]}}

    @field_validator('period')
    @classmethod
    def _reject_all_time(cls, value: PublishTime) -> PublishTime:
        # AFTER coercion (not `mode='before'`), so the check runs on an enum
        # member and cannot be fooled by `0` vs `'0'`.
        if value is PublishTime.ALL_TIME:
            raise ValueError(POSTS_PERIOD_ALL_TIME_MSG)
        return value


class UserPostsResponse(BaseModel):
    username: str = Field(description='The handle that was asked for, normalised (no leading `@`).')
    user_id: Optional[str] = Field(default=None, description='Echoed from the request when it supplied one, else read off the first kept record. Null when the request supplied none and this page kept nothing.')
    sec_uid: Optional[str] = Field(default=None, description='Same provenance as `user_id`.')
    # Neither carries a Pydantic default, deliberately: a defaulted field is
    # omitted from OpenAPI's `required` list, and a generated client would then
    # type it optional-and-possibly-missing — exactly the doc-footnote status
    # these two fields exist to avoid. The handler passes both explicitly.
    source: str = Field(description="Where these posts came from, machine-readably. `search` means they are the videos KEYWORD SEARCH surfaces for this handle, filtered down to the ones this account authored — not a read of the account's post feed. Treat any other value as a different (better) source.")
    complete: bool = Field(description="Whether this is the account's full post history. **Always false on the `search` source**, now for three independent reasons: the set is only what keyword search SURFACES (posts the account really has may never appear); it is bounded to `period`, so older posts are excluded BY DESIGN; and TikTok's index is not deterministic — the same keyword in the same window measured 17 records on one call and 14 on another. Coverage, not completeness, is what the multi-keyword union buys (see `keywords`). Yield tracks how visible the account is in search, so a low-visibility account can legitimately return few results or none. COMPLETENESS only: `results` is ordered newest-first (see `results`), so a short page is still a correctly ordered one.")
    period: PublishTime = Field(description='The recency window that was searched, echoed back. Every record in `results` is from within it, so a caller can state the range it is looking at without inferring it from the records.')
    keywords: list[str] = Field(description="The keywords that were actually searched, in the order they were spent, so a caller can see WHY it got what it got. Always starts with the handle; the account's `display_name` follows when it adds something (it is omitted when absent, or equal to the handle case-insensitively). Each entry is one search, one `cap_units` unit, and its own `source_term` on the records it surfaced. Measured live on `@bakuesaz` in the default window: `bakuesaz` alone surfaced 14 of the returned posts and `BAKU ES` (its display name) another 10, for 24 across 16 distinct days — the second keyword is a real coverage gain, not a spelling variant. Nothing here is guessed from how the handle might split into words.")
    cap_units: int = Field(description='What this request COST against the per-device daily cap. The cap is charged per pooled call, not per signed request, so this is one unit per entry in `keywords` plus one for the display-name resolve — i.e. `len(keywords) + 1` on a first page, and exactly `1` on a `page_token` continuation, which skips the resolve and searches the handle alone. Stated rather than hidden: the caller is paying for coverage and must be able to see the price. Each of those calls may itself spend several signed requests across the two merged video endpoints — that is the pre-existing `/search` cost model and is NOT charged again against the cap.')
    device: str = Field(description='Label(s) of the device(s) that served this request, `+`-joined and de-duplicated: one pooled call per keyword plus the resolve, each independently scheduled, so on a multi-device pool they need not be the same device.')
    count: int = Field(description='How many records `results` carries — i.e. AFTER dropping the search hits by other authors and unioning the keywords. Capped by `limit`, normally well below it, and can be 0 while `has_more` is true: the filter runs after the pages are fetched, so an unlucky page can be entirely other people. A nonexistent handle answers `404` here (the display-name resolve looks it up), so `count: 0` now means "this account surfaced nothing in this window" — try a wider `period` before concluding anything.')
    has_more: bool = Field(description='Whether any of the searched keywords has more pages. Judged on the unfiltered pages, so it stays true even when `count` is 0. Usually FALSE even on a large account: a `period` window typically exhausts within one request, which is why deeper paging is not where coverage comes from here — extra keywords are.')
    page_token: Optional[str] = Field(default=None, description='Pass this back as `page_token` for the next page. Null when there is nothing more to fetch **and also whenever more than one keyword was searched**: a page merged from several queries has no single search session to resume, so no token is minted rather than one that would resume a different query than it was minted for. A single-keyword page (no usable display name) still gets one.')
    elapsed_s: float
    results: list[dict[str, Any]] = Field(description="Video records in the IDENTICAL shape `/search` returns (`flatten_video`), so a client renders them with the same code. De-duplicated by record `id` across the searched keywords, and each record's `source_term` names the keyword that surfaced it. **Ordered newest first by `create_time`** — records whose `create_time` is null (unknown upstream) come LAST, since an undated post cannot be truthfully placed among dated ones. The order is stable, so identical requests return identical orderings. **This orders the PAGE, not the paginated STREAM:** pages still arrive in search-relevance order, so page 2 can contain videos both older AND newer than page 1. Read one page and you get a correctly ordered page; paginate and you must ACCUMULATE and RE-SORT across pages yourself.")
