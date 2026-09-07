from __future__ import annotations
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import partial
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from ..client import SEARCH_PATHS
from ..config import RAPIDAPI_KEY_ENV, PoolConfig
from ..errors import PoolCode, PoolExhausted, RateLimited, SoftError, TransportError
from ..filters import SearchFilters, SearchPage, SearchQuery
from ..identity_manager import IdentityStore
from ..paging import TOKEN_VERSION, PageToken, decode, encode, query_hash
from ..pool import ClientPool
from .schemas import HealthResponse, SearchRequest, SearchResponse
logger = logging.getLogger('tiktoksearch.api')
DEFAULT_CONFIG_PATH = 'config_signed.yaml'
# Optional hot-reloadable warm-identity file. Env override wins; else config's
# `identities_path`; else a conventional default next to the config.
IDENTITIES_ENV = 'TIKTOK_IDENTITIES_PATH'
# Startup misconfiguration banners. Both are logged loudly (ERROR) and never
# raise: config_signed.yaml is a legitimate legacy profile with no rapidapi_key.
# Neither message may carry key material — they state absence only.
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
NO_SIGNER_KEY_MSG = (
    'No signer key configured: %s is unset/empty in the environment and '
    '`rapidapi_key` is absent from the config profile. Requests will run the COLD '
    'legacy signer path, which returns empty results BY DESIGN. An empty result in '
    'this state is a configuration problem, not hit_shark risk-control. Set %s in '
    '.env (see .env.example) and restart.'
)

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
    if not page.has_more or not page.endpoints:
        return None
    return encode(PageToken(version=TOKEN_VERSION, query_hash=_query_hash(query.kind.value, query.term, query.filters), device_handle=handle, endpoints=page.endpoints, seen=page.seen))

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
        if not config.client_defaults.rapidapi_key:
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
        try:
            if fan_out > 1:
                devices, page = await loop.run_in_executor(None, pool.run_merged, query, fan_out)
                device = '+'.join(devices)
            else:
                handle = query.page_token.device_handle if query.page_token is not None else None
                served, page = await loop.run_in_executor(None, partial(pool.run, query, handle=handle))
                device = served.label
                next_token = _next_page_token(query, served.handle, page)
        except PoolExhausted as exc:
            # Map on the CODE, never on the prose: rewording a pool message
            # must not be able to flip the HTTP status.
            status = 429 if exc.code is PoolCode.CAP else 503
            raise HTTPException(status_code=status, detail=exc.reason) from exc
        except RateLimited as exc:
            raise HTTPException(status_code=429, detail='TikTok rate-limited the request — slow down or add proxies.') from exc
        except (SoftError, TransportError) as exc:
            raise HTTPException(status_code=502, detail=f'TikTok request failed: {exc}') from exc
        return SearchResponse(query=query.term, type=req.type, device=device, count=len(page.records), cursor=page.cursor, next_cursor=page.next_cursor, page_token=next_token, has_more=page.has_more, elapsed_s=round(time.monotonic() - started, 2), results=page.records)

    @app.get('/health', response_model=HealthResponse, tags=['ops'])
    async def health(pool: ClientPool=Depends(get_pool)) -> HealthResponse:
        status = pool.status()
        status['status'] = 'ok' if status['device_count'] > 0 else 'no_devices'
        return HealthResponse(**status)
    return app
