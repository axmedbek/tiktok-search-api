from __future__ import annotations
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from ..config import PoolConfig
from ..errors import PoolExhausted, RateLimited, SoftError, TransportError
from ..filters import SearchFilters, SearchQuery
from ..pool import ClientPool
from .schemas import HealthResponse, SearchRequest, SearchResponse
logger = logging.getLogger('tiktoksearch.api')
DEFAULT_CONFIG_PATH = 'config_signed.yaml'

def _to_query(req: SearchRequest, max_results: int) -> SearchQuery:
    filters = SearchFilters(sort_type=req.filters.sort_type if req.filters else None, publish_time=req.filters.publish_time if req.filters else None)
    try:
        return SearchQuery(kind=req.type, term=req.query, limit=min(req.limit, max_results), cursor=req.cursor, filters=filters)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

def get_pool(request: Request) -> ClientPool:
    return request.app.state.pool

def create_app(config_path: str=DEFAULT_CONFIG_PATH) -> FastAPI:
    config = PoolConfig.load_yaml(config_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = ClientPool(config)
        app.state.config = config
        status = app.state.pool.status()
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
