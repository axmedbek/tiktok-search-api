from __future__ import annotations
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from ..config import PoolConfig
from ..errors import PoolExhausted, RateLimited, SoftError, TransportError
from ..filters import SearchFilters, SearchQuery
from ..identity_manager import IdentityStore
from ..pool import ClientPool
from .schemas import HealthResponse, SearchRequest, SearchResponse
logger = logging.getLogger('tiktoksearch.api')
DEFAULT_CONFIG_PATH = 'config_signed.yaml'
# Optional hot-reloadable warm-identity file. Env override wins; else config's
# `identities_path`; else a conventional default next to the config.
IDENTITIES_ENV = 'TIKTOK_IDENTITIES_PATH'

def _to_query(req: SearchRequest, max_results: int) -> SearchQuery:
    filters = SearchFilters(sort_type=req.filters.sort_type if req.filters else None, publish_time=req.filters.publish_time if req.filters else None)
    try:
        return SearchQuery(kind=req.type, term=req.query, limit=min(req.limit, max_results), cursor=req.cursor, filters=filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

def get_pool(request: Request) -> ClientPool:
    return request.app.state.pool

def _resolve_identities_path(config_path: str) -> str | None:
    """Where to read hot-reloadable warm identities from, if anywhere."""
    env = os.environ.get(IDENTITIES_ENV)
    if env:
        return env
    # config yaml may carry `identities_path`
    try:
        import yaml
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f) or {}
            p = raw.get('identities_path')
            if p:
                return p
    except Exception:  # pragma: no cover - config parsing already validated elsewhere
        pass
    # conventional default alongside the config
    default = os.path.join(os.path.dirname(os.path.abspath(config_path)) or '.', 'identities.json')
    return default if os.path.exists(default) else None


def create_app(config_path: str=DEFAULT_CONFIG_PATH) -> FastAPI:
    config = PoolConfig.load_yaml(config_path)
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
        try:
            if fan_out > 1:
                devices, page = await loop.run_in_executor(None, pool.run_merged, query, fan_out)
                device = '+'.join(devices)
            else:
                device, page = await loop.run_in_executor(None, pool.run, query)
        except PoolExhausted as exc:
            code = 429 if 'cap reached' in exc.reason else 503
            raise HTTPException(status_code=code, detail=exc.reason) from exc
        except RateLimited as exc:
            raise HTTPException(status_code=429, detail='TikTok rate-limited the request — slow down or add proxies.') from exc
        except (SoftError, TransportError) as exc:
            raise HTTPException(status_code=502, detail=f'TikTok request failed: {exc}') from exc
        return SearchResponse(query=query.term, type=req.type, device=device, count=len(page.records), cursor=page.cursor, next_cursor=page.next_cursor, has_more=page.has_more, elapsed_s=round(time.monotonic() - started, 2), results=page.records)

    @app.get('/health', response_model=HealthResponse, tags=['ops'])
    async def health(pool: ClientPool=Depends(get_pool)) -> HealthResponse:
        status = pool.status()
        status['status'] = 'ok' if status['device_count'] > 0 else 'no_devices'
        return HealthResponse(**status)
    return app
