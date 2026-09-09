from __future__ import annotations
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Iterator
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from ..client import SEARCH_ITEM_PATH, SEARCH_PATHS, SEARCH_VIDEO_PATH, TikTokClient
from ..config import RAPIDAPI_KEY_ENV, SIGNER_LEGACY, SIGNER_RAPID, PoolConfig
from ..errors import NotFound, PoolCode, PoolExhausted, RateLimited, SoftError, TransportError
from ..filters import SearchFilters, SearchKind, SearchPage, SearchQuery
from ..identity_manager import IdentityStore
from ..paging import TOKEN_VERSION, PageToken, decode, encode, query_hash
from ..pool import CallOutcome, ClientPool, HealthVerdict, posts_verdict
from .schemas import POSTS_COMPLETE, POSTS_SOURCE, PROFILE_SOURCE, HealthResponse, ProfileRequest, ProfileResponse, SearchRequest, SearchResponse, UserPostsRequest, UserPostsResponse
logger = logging.getLogger('tiktoksearch.api')
DEFAULT_CONFIG_PATH = 'config_signed.yaml'
# Optional hot-reloadable warm-identity file. Env override wins; else config's
# `identities_path`; else a conventional default next to the config.
IDENTITIES_ENV = 'TIKTOK_IDENTITIES_PATH'
# Startup misconfiguration banners. MISSING_CONFIG_MSG and NO_SIGNER_KEY_MSG are
# logged loudly (ERROR) and never raise: config_signed.yaml is a legitimate
# legacy profile with no rapidapi_key, and the cold path it selects does still
# start and serve. RAPID_WITHOUT_KEY_MSG is the one that DOES raise — see
# create_app. No message may carry key material — they state absence only.
MISSING_CONFIG_MSG = (
    'Config file not found: %s — the API is running on built-in defaults '
    '(no configured devices, no warm identity, and the %s env override is NOT '
    'applied on this path). In a container this means the config bind mount is '
    'missing or misnamed.'
)
FAN_OUT_NO_TOKEN_MSG = (
    'default_fan_out is %d on this profile: a plain request fans out across '
    'devices and merges, so it cannot be continued — no page_token is minted '
    'for it. Callers that need token pagination must send fan_out=1.'
)
# A page_token names ONE device's search session; fanning out would merge pages
# from other devices whose sessions the token knows nothing about. Silently
# overriding what the caller explicitly asked for is worse than refusing.
FAN_OUT_TOKEN_CONFLICT_MSG = (
    'fan_out > 1 cannot be combined with page_token: a search session lives on '
    'a single device. Send fan_out=1 or drop page_token.'
)
# Which signer is live, so `docker compose logs` answers it without guesswork.
# Args are the resolved mode and the raw `signer:` value — both are mode names,
# never key material.
SIGNER_MODE_MSG = 'Signer: %s (config `signer:` = %s).'
# Fires ONLY for the resolved `legacy` mode — the one keyless mode that still
# starts, since a keyless `rapid` is refused in create_app and `local` needs no
# key at all — so every claim here is true for the single branch that reaches it.
# The remedy names BOTH exits on purpose: an explicitly configured
# `signer: legacy` ignores the env key, so setting it alone would change nothing.
NO_SIGNER_KEY_MSG = (
    'No signer key configured: %s is unset/empty in the environment and '
    '`rapidapi_key` is absent from the config profile, so the resolved signer is '
    'the COLD legacy path, which returns empty results BY DESIGN. An empty result '
    'in this state is a configuration problem, not hit_shark risk-control. Set '
    '`signer: local` in the config profile for free in-process v46 signing, or '
    '`signer: rapid` plus %s in .env (see .env.example), and restart.'
)
# The fatal one. `signer: rapid` is an explicit request for the paid signer, and
# RapidSigner raises on construction without a key, so EVERY client in the pool
# would fail to build — the failure is a configuration error and is named as one.
RAPID_WITHOUT_KEY_MSG = (
    'Config `signer: rapid` selects the paid RapidAPI signer, but no key is '
    'configured: %s is unset/empty in the environment and `rapidapi_key` is '
    'absent from the config profile. That signer cannot sign a single request '
    'without a key, so the server refuses to start rather than fail every '
    'search. Set %s in .env (see .env.example), or switch the profile to '
    '`signer: local` for free in-process v46 signing. No other signer is '
    'substituted: `signer: rapid` is the stale-sign-key diagnostic, and quietly '
    'serving a different one would destroy the signal it exists to give.'
)
# `/user/posts` is served BY the keyword-search path on the current contract, so
# a posts page_token names SEARCH endpoint paths — a posts-only allow-list would
# reject every token this endpoint itself mints. (`client.USER_POSTS_PATH`
# belongs here only once that endpoint is what actually serves the records.)
# It is the two VIDEO paths and not `SEARCH_PATHS`, because `_to_posts_query`
# hardcodes `SearchKind.KEYWORD`: nothing this endpoint mints can ever name
# `SEARCH_USER_PATH`, and an allow-list whose whole job is narrowness must not
# admit a path that is unreachable by construction. The path inside a token
# ends up in a signed TikTok URL, which is what makes it an ALLOW-list.
POSTS_TOKEN_PATHS: frozenset[str] = frozenset((SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH))
# ...but a posts token must still not be interchangeable with a `/search` one,
# even though they name the same paths and would resume the same stream: the
# two endpoints filter and shape their pages differently, and a token that
# silently works on both is a contract nobody chose. The query hash is
# namespaced on this kind string — which is deliberately NOT a SearchKind
# value, so it cannot collide with one.
POSTS_QUERY_KIND = 'user_posts'
# Client-facing failure text for the two new endpoints. Terse, and carrying
# nothing about the identity, the device or the upstream reply.
NO_SUCH_USER_DETAIL = 'No TikTok user matches that username.'
UNMINTABLE_TOKEN_DETAIL = 'TikTok returned a page that cannot be continued — retry the request.'


def _query_hash(kind: str, term: str, filters: SearchFilters) -> str:
    return query_hash(kind, term, filters.to_query_params())

def _to_query(req: SearchRequest, max_results: int) -> SearchQuery:
    filters = SearchFilters(sort_type=req.filters.sort_type if req.filters else None, publish_time=req.filters.publish_time if req.filters else None)
    token: PageToken | None = None
    if req.page_token:
        # An explicit fan_out > 1 alongside a token is a contradiction, not
        # something to silently rewrite (the server DEFAULT is coerced instead —
        # the caller asked for nothing there).
        if req.fan_out is not None and req.fan_out > 1:
            raise HTTPException(status_code=422, detail=FAN_OUT_TOKEN_CONFLICT_MSG)
        # A malformed / unauthenticated / wrong-version / foreign-query token is
        # a CLIENT error: 422, never a SoftError/502. The ValueError messages
        # from paging.py are deliberately terse and carry no internals.
        try:
            token = decode(req.page_token, expected_query_hash=_query_hash(req.type.value, req.query.strip(), filters), allowed_paths=SEARCH_PATHS)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return SearchQuery(kind=req.type, term=req.query, limit=min(req.limit, max_results), cursor=req.cursor, filters=filters, page_token=token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

def _next_page_token(query: SearchQuery, handle: str, page: SearchPage) -> str | None:
    """Mint the continuation token for the page just served. None when there is
    nothing more to fetch, or when the page carries no resumable end state.

    `page.seen` rides along so the NEXT request can seed its dedup set: it is
    what keeps a second endpoint resuming its own deeper window from re-emitting
    records this page already served."""
    return _mint_token(_query_hash(query.kind.value, query.term, query.filters), handle, page, kind=query.kind.value)


def _mint_token(query_hash_value: str, handle: str, page: SearchPage, *, kind: str) -> str | None:
    """`_next_page_token`'s body, minus which query the token is bound to.

    Split out because `/user/posts` mints from the same page shape but under
    its own namespaced hash — one encoder, two bindings, rather than a second
    copy of the PageToken construction.

    The `ValueError` net lives HERE, in the one shared body, so both endpoints
    are covered by one net rather than one of them being covered and the other
    500-ing. It is defence in depth and not a live path: `paging.encode` raises
    rather than trims when a cursor or the wire form is out of bounds, and
    every producer of those values is already bounded (`paging.cursor_bound` on
    the way in, `MAX_PAGE_TOKEN_CHARS` derived from the worst case `encode` can
    emit). If it ever does fire, 502 and not 422: the caller's input was valid
    and the page was already served — it is the upstream cursor that is odd.
    It is NOT swallowed into a 200 with a null token, which would hand back an
    unresumable page and hide a producer bug in `paging.encode`.

    `kind` is for the log line only — it names which endpoint/search kind hit
    it, which is the first thing needed to reproduce a producer bug."""
    if not page.has_more or not page.endpoints:
        return None
    try:
        return encode(PageToken(version=TOKEN_VERSION, query_hash=query_hash_value, device_handle=handle, endpoints=page.endpoints, seen=page.seen))
    except ValueError as exc:
        logger.warning('could not mint a continuation token (kind=%s): %s', kind, exc)
        raise HTTPException(status_code=502, detail=UNMINTABLE_TOKEN_DETAIL) from exc


def _posts_query_hash(username: str) -> str:
    """The query identity a `/user/posts` token is bound to: this handle, under
    POSTS_QUERY_KIND, with no filters (the endpoint accepts none).

    Lower-cased, because TikTok handles are case-insensitive and the boundary
    normalisation (`_HandleRequest`) strips `@` and whitespace but NOT case.
    Without this, page 1 asked for as `@BakuEsaz` and page 2 as `@bakuesaz`
    hash differently and the token 422s — a fail-closed but baffling rejection
    for a caller that varies capitalisation between pages. It is the same
    lower-cased comparison `_record_handle` / `_authored_by` already make, so
    the token binding now matches the filtering it resumes.

    Mint and decode both go through HERE, which is what makes the two agree by
    construction. The handle is lower-cased for the HASH only: `query.term`
    (what reaches the signed URL) and the echoed `username` stay in the
    caller's normalised spelling."""
    return query_hash(POSTS_QUERY_KIND, username.lower(), {})


def _to_posts_query(req: UserPostsRequest, max_results: int) -> SearchQuery:
    """The keyword search that serves `/user/posts`: the handle as the term.

    `req.username` is already normalised and charset-bounded by
    `_HandleRequest`, so what reaches the signed URL is what the boundary
    admitted."""
    token: PageToken | None = None
    if req.page_token:
        # Same contract as `/search`: a malformed / unauthenticated /
        # wrong-version / foreign token is a CLIENT error (422), never a 502.
        # A `/search` token fails HERE, on the query hash, because of
        # POSTS_QUERY_KIND.
        try:
            token = decode(req.page_token, expected_query_hash=_posts_query_hash(req.username), allowed_paths=POSTS_TOKEN_PATHS)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return SearchQuery(kind=SearchKind.KEYWORD, term=req.username, limit=min(req.limit, max_results), filters=SearchFilters(), page_token=token)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _mint_posts_token(query: SearchQuery, handle: str, page: SearchPage) -> str | None:
    """The continuation token for a posts page, or None when there is no more.

    Binds the page to THIS handle under POSTS_QUERY_KIND; the out-of-bounds
    `ValueError` net that used to sit here now lives in `_mint_token`, shared
    with `/search`."""
    return _mint_token(_posts_query_hash(query.term), handle, page, kind=POSTS_QUERY_KIND)


def _record_handle(record: dict) -> str | None:
    """A video record's author handle, lower-cased, or None if it has none.

    Reads `author_unique_id` — the IDENTITY field — and NOT `author_username`,
    which `mapping.flatten_video` fills from `nickname` when the author has no
    handle. A nickname is user-settable and not unique, so comparing it lets
    any account that names itself `bakuesaz` be served as `@bakuesaz`, ids and
    all. A record with no handle returns None, which compares equal to no
    wanted handle at all — not even an empty one."""
    value = record.get('author_unique_id')
    return value.lower() if isinstance(value, str) and value else None


def _authored_by(records: list[dict], username: str) -> list[dict]:
    """Only the records this handle authored, compared case-insensitively.

    Server-side rather than client-side because the interim contract is "this
    account's posts": handing back the other authors' videos too would make
    `/user/posts` a differently-named `/search`. Measured on `@bakuesaz`: 48 of
    60 first-page records were the author's.

    The comparison is on the identity field only — see `_record_handle`. A
    record whose author carries no handle is DROPPED, never matched."""
    wanted = username.lower()
    return [record for record in records if _record_handle(record) == wanted]


# A record whose `create_time` is unknown sorts under this key. It is the empty
# string because it compares LESS than every ISO-8601 timestamp, so under
# `reverse=True` those records land LAST — see `_newest_first`.
UNKNOWN_CREATE_TIME_KEY = ''


def _create_time_key(record: dict) -> str:
    """A video record's sort key: its `create_time` string, or the unknown-date
    sentinel.

    A plain STRING key, not a parsed `datetime`, and that is provable rather
    than lucky: `mapping._iso_utc` renders every timestamp as
    `datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()`, whose input is
    an `int` (so never a fractional part) and whose offset is always `+00:00`.
    That is the fixed 25-character form `YYYY-MM-DDTHH:MM:SS+00:00`, with the
    year zero-padded by `isoformat` even below 1000 — equal width, same
    offset, zero-padded fields, therefore lexicographic order IS chronological
    order. Parsing with `datetime.fromisoformat` would buy nothing and add a
    `ValueError` path on a value that has already been served in the records.

    Anything that is not a non-empty string (`None` from a zero, absent or
    unrenderable upstream `create_time`, or a type that could not occur through
    `flatten_video`) gets the sentinel, so no comparison can ever be attempted
    between a string and a non-string."""
    value = record.get('create_time')
    return value if isinstance(value, str) and value else UNKNOWN_CREATE_TIME_KEY


def _newest_first(records: list[dict]) -> list[dict]:
    """`records` ordered newest-first by `create_time`.

    Records with an unknown `create_time` go to the END. A record with no date
    cannot be truthfully placed among dated ones, and the front of a
    newest-first list is the strongest claim the response makes — putting an
    undated record there would assert it is the most recent post, which is a
    fabrication. At the back it is visibly "the leftovers", and the dated
    prefix stays strictly newest-first.

    STABLE: `sorted` is stable and stays so under `reverse=True`, so records
    sharing a timestamp — and the whole undated tail — keep the order the
    upstream page gave them. That is what makes two identical requests return
    byte-identical orderings, which is what the UI's accumulate-and-re-sort
    and any client-side caching rely on.

    This orders THIS PAGE. It does NOT make the paginated stream globally
    sorted — pages still arrive in search-relevance order. `UserPostsResponse`
    documents that for callers."""
    return sorted(records, key=_create_time_key, reverse=True)


def _posts_ids(req: UserPostsRequest, records: list[dict]) -> tuple[str | None, str | None]:
    """The `(user_id, sec_uid)` to report for a posts page.

    What the caller supplied wins, so the pair stays stable across pages. With
    nothing supplied they are read off the first record of `records` — real ids
    out of the reply, never invented — and stay null on a page that kept
    nothing. The handler passes `_newest_first(_authored_by(...))`, so "first"
    means the NEWEST kept record; which record it is does not matter to the
    guarantee below, only that it came from that filtered set.

    `records` is `_authored_by`'s output (possibly reordered), so every record
    here matched the handle on the identity field. That is what makes reading ids off one of
    them safe: ids adopted from a record that matched on a display field would
    be a DIFFERENT account's identity, handed back as this handle's."""
    first = records[0] if records else {}
    return (req.user_id or first.get('author_id'), req.sec_uid or first.get('author_sec_uid'))


@contextmanager
def _domain_errors() -> Iterator[None]:
    """Map the domain exceptions to HTTP. THE map — every handler uses it.

    One map and not one per handler (`.claude/rules/api-service.md`): a
    hand-written copy that forgets a class lets it escape `run_in_executor` as
    a 500, and `NotFound` is exactly the kind of class a second copy drops.

    `/search` is inside it too. That is behaviour-preserving, not a widening:
    its block also covers `_next_page_token`, which raises only `HTTPException`
    (its own 502 — see `_mint_token`) and, unchanged, whatever the pool call
    raises. No clause here catches `HTTPException`, so that 502 reaches the
    client verbatim rather than being re-mapped into the `SoftError` prose.
    The one class this adds to `/search` is `NotFound`, which nothing on the
    search path raises.

    No exception's own text reaches the client except the ones already chosen
    for that: `PoolExhausted.reason` (operational, no internals) and the
    pre-existing `SoftError`/`TransportError` prose."""
    try:
        yield
    except PoolExhausted as exc:
        # Map on the CODE, never on the prose — see `/search`.
        status = 429 if exc.code is PoolCode.CAP else 503
        raise HTTPException(status_code=status, detail=exc.reason) from exc
    except RateLimited as exc:
        raise HTTPException(status_code=429, detail='TikTok rate-limited the request — slow down or add proxies.') from exc
    except NotFound as exc:
        # 404, and NOT the 502 an unmapped domain error would become: TikTok
        # answered, the identity is fine, and the username simply is not there.
        raise HTTPException(status_code=404, detail=NO_SUCH_USER_DETAIL) from exc
    except (SoftError, TransportError) as exc:
        raise HTTPException(status_code=502, detail=f'TikTok request failed: {exc}') from exc

def get_pool(request: Request) -> ClientPool:
    return request.app.state.pool

def _resolve_identities_path(config_path: str) -> str | None:
    """Where to read hot-reloadable warm identities from, if anywhere."""
    env = os.environ.get(IDENTITIES_ENV)
    if env:
        return env
    config_dir = os.path.dirname(os.path.abspath(config_path)) or '.'
    # config yaml may carry `identities_path` — a relative path is resolved
    # against the CONFIG's directory, not the process cwd, so the server works
    # no matter where it is launched from.
    try:
        import yaml
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f) or {}
            p = raw.get('identities_path')
            if p:
                return p if os.path.isabs(p) else os.path.join(config_dir, p)
    except Exception:  # pragma: no cover - config parsing already validated elsewhere
        pass
    # conventional default alongside the config
    default = os.path.join(config_dir, 'identities.json')
    return default if os.path.exists(default) else None


def create_app(config_path: str=DEFAULT_CONFIG_PATH) -> FastAPI:
    config = PoolConfig.load_yaml(config_path)
    # load_yaml falls back to all-defaults for a missing path; remember that so
    # startup can say so out loud (its return contract stays unchanged).
    config_missing = not os.path.exists(config_path)
    identities_path = _resolve_identities_path(config_path)
    client_defaults = config.client_defaults
    signer_mode = client_defaults.resolved_signer()
    # Resolved HERE, before any client is constructed, because a keyless `rapid`
    # is fatal: RapidSigner raises on construction, so ClientPool would take the
    # process down with a bare ValueError before startup reached any banner.
    if signer_mode == SIGNER_RAPID and not client_defaults.rapidapi_key:
        logger.error(RAPID_WITHOUT_KEY_MSG, RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV)
        raise ValueError(RAPID_WITHOUT_KEY_MSG % (RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        identities = IdentityStore(identities_path) if identities_path else None
        app.state.identities = identities
        app.state.pool = ClientPool(config, identities=identities)
        app.state.config = config
        status = app.state.pool.status()
        if identities is not None:
            logger.info('Warm-identity store: %s (%d usable).', identities_path, identities.usable_count())
        logger.info('Signed search API up. %d device(s), total capacity %d/day.', status['device_count'], status['total_daily_capacity'])
        if config.default_fan_out > 1:
            logger.warning(FAN_OUT_NO_TOKEN_MSG, config.default_fan_out)
        if config_missing:
            logger.error(MISSING_CONFIG_MSG, config_path, RAPIDAPI_KEY_ENV)
        logger.info(SIGNER_MODE_MSG, signer_mode, client_defaults.signer or 'unset')
        # The banner is about running a path that CANNOT return results: `legacy`
        # — what an unset `signer:` with no key resolves to — is the cold path
        # that returns empty BY DESIGN, the silent-empty misconfiguration this
        # banner exists for. `rapid` without a key never gets here (create_app
        # refused it), and in `local` mode a missing key is normal: local signing
        # is free, and RapidAPI is only the narrow fallback for a failed local
        # SIGNING call.
        if signer_mode == SIGNER_LEGACY and not client_defaults.rapidapi_key:
            logger.error(NO_SIGNER_KEY_MSG, RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV)
        yield
        logger.info('Signed search API shutting down.')
    app = FastAPI(title='TikTok Mobile Search API', version='2.0', summary="Signed direct access to TikTok's mobile search — no phone, no login.", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

    @app.post('/search', response_model=SearchResponse, tags=['search'])
    async def search(req: SearchRequest, pool: ClientPool=Depends(get_pool)) -> SearchResponse:
        query = _to_query(req, config.max_results_per_search)
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        fan_out = req.fan_out if req.fan_out is not None else config.default_fan_out
        # A search session lives on ONE device, so a continuation cannot fan
        # out. An EXPLICIT fan_out > 1 was already rejected in _to_query; this
        # only coerces the server default, which the caller never asked for.
        if query.page_token is not None:
            fan_out = 1
        next_token: str | None = None
        # `_next_page_token` is INSIDE the block, as it has always been. It maps
        # nothing (it raises only ValueError, which `_domain_errors` does not
        # catch) — it is in here because it reads `served`, which only exists
        # on this branch.
        with _domain_errors():
            if fan_out > 1:
                devices, page = await loop.run_in_executor(None, pool.run_merged, query, fan_out)
                device = '+'.join(devices)
            else:
                handle = query.page_token.device_handle if query.page_token is not None else None
                served, page = await loop.run_in_executor(None, partial(pool.run, query, handle=handle))
                device = served.label
                next_token = _next_page_token(query, served.handle, page)
        return SearchResponse(query=query.term, type=req.type, device=device, count=len(page.records), cursor=page.cursor, next_cursor=page.next_cursor, page_token=next_token, has_more=page.has_more, elapsed_s=round(time.monotonic() - started, 2), results=page.records)

    @app.post('/profile', response_model=ProfileResponse, tags=['user'])
    async def profile(req: ProfileRequest, pool: ClientPool=Depends(get_pool)) -> ProfileResponse:
        """One user's profile, by handle, in ONE signed request.

        The profile is flattened from the user-search node that a resolve would
        have paid for anyway, so this costs exactly one signed request and one
        daily-cap unit — see `client.profile_from_search`, which also documents
        why `signature` and `region_code` are always null here."""
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        username = req.username

        def call(client: TikTokClient) -> CallOutcome[dict]:
            record = client.profile_from_search(username)
            # Reaching here means the reply carried a populated `user_list`
            # AND an exact handle match that flattened, so the identity is
            # trusted. The other outcomes never arrive as a result: an empty
            # `user_list` is a SoftError (reported as an empty by `run_call`),
            # no exact match is NotFound and a `uid`-less node is a
            # TransportError — neither of which charges the identity.
            return CallOutcome(result=record, verdict=HealthVerdict.OK)

        # ONE run_call, therefore ONE daily-cap unit: the cap is charged per
        # run_call, not per signed request, so a second signed request inside
        # this `fn` would ride the cap for free and break the cost model.
        with _domain_errors():
            served, record = await loop.run_in_executor(None, partial(pool.run_call, call))
        # The keys are `mapping.flatten_profile`'s contract, which is what
        # ProfileResponse was written against.
        # `source` is passed explicitly rather than defaulted in the schema: a
        # Pydantic default keeps the field out of OpenAPI's `required` list, so
        # a generated client types it optional-and-possibly-missing — the
        # doc-footnote status it exists to escape.
        return ProfileResponse(**record, source=PROFILE_SOURCE, device=served.label, elapsed_s=round(time.monotonic() - started, 2))

    @app.post('/user/posts', response_model=UserPostsResponse, tags=['user'])
    async def user_posts(req: UserPostsRequest, pool: ClientPool=Depends(get_pool)) -> UserPostsResponse:
        """The videos search surfaces for one handle, filtered to that author.

        NOT a timeline and NOT the account's full history — `source` and
        `complete` say so in the response, and `UserPostsResponse` explains
        both.

        ONE `run_call` per page, therefore ONE daily-cap unit per page: the cap
        is charged per run_call, not per signed request. Inside it, this pays
        exactly what `/search` pays for the same `limit` — it IS `client.search`
        — so a page may spend several signed requests on the two merged video
        endpoints. That is the pre-existing search cost model, unchanged;
        what must not happen is stacking a SECOND logical call (a resolve, say)
        into this one, which would ride the same cap unit for free.

        No fan-out, ever: a merged multi-device page cannot be resumed (it has
        no single search session to pin) and it would spend one cap unit per
        device on a page this endpoint then filters down anyway."""
        query = _to_posts_query(req, config.max_results_per_search)
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        # A search session lives on ONE device, so a continuation is pinned to
        # the device that served the previous page — exactly as in `/search`.
        handle = query.page_token.device_handle if query.page_token is not None else None

        def call(client: TikTokClient) -> CallOutcome[SearchPage]:
            page = client.search(query)
            return CallOutcome(result=page, verdict=posts_verdict(page))

        with _domain_errors():
            served, page = await loop.run_in_executor(None, partial(pool.run_call, call, handle=handle))
        # Filter, then order newest-first — one canonical ordering that
        # everything downstream (`_posts_ids`, `results`) reads. The sort is a
        # PERMUTATION of `_authored_by`'s output, so `_posts_ids`' guarantee is
        # untouched: every record it can read ids off still matched the handle
        # on the identity field.
        records = _newest_first(_authored_by(page.records, req.username))
        # Outside `_domain_errors` on purpose: this raises HTTPException, which
        # must reach the client as-is and never be re-mapped.
        next_token = _mint_posts_token(query, served.handle, page)
        user_id, sec_uid = _posts_ids(req, records)
        # `count` is post-filter and `has_more` is pre-filter, so count=0 with
        # has_more=true is a normal answer, not a contradiction — a page whose
        # hits were all other authors. Documented on both fields.
        # `source` / `complete` passed explicitly, for the reason given in
        # `/profile` above: a schema default is not a REQUIRED field, and these
        # two exist precisely so the interim nature is machine-readable.
        return UserPostsResponse(username=req.username, user_id=user_id, sec_uid=sec_uid, source=POSTS_SOURCE, complete=POSTS_COMPLETE, device=served.label, count=len(records), has_more=page.has_more, page_token=next_token, elapsed_s=round(time.monotonic() - started, 2), results=records)

    @app.get('/health', response_model=HealthResponse, tags=['ops'])
    async def health(pool: ClientPool=Depends(get_pool)) -> HealthResponse:
        status = pool.status()
        status['status'] = 'ok' if status['device_count'] > 0 else 'no_devices'
        return HealthResponse(**status)
    return app
