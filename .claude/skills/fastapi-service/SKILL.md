---
name: fastapi-service
description: The HTTP API layer — endpoints, Pydantic schemas, domain→HTTP error mapping, running and serving via uvicorn/tunnel — use for FastAPI, endpoint, /search, /health, uvicorn, schema, Pydantic, HTTP status, serve, deploy, tunnel, or CORS work.
---

# FastAPI Service

The HTTP layer lives in `mobile/tiktoksearch/api/`: `app.py` (factory + routes + error map) and `schemas.py` (Pydantic models). Entry: `mobile/api_signed.py` calls `create_app(config_path)`.

## create_app factory + identities path
`create_app(config_path=DEFAULT_CONFIG_PATH)` in `api/app.py` builds the pool and routes. Warm-identity path is resolved by `_resolve_identities_path` with this precedence:
1. env `TIKTOK_IDENTITIES_PATH` (`IDENTITIES_ENV`)
2. `identities_path:` key in the config yaml — a **relative** value is resolved relative to the config file's directory
3. else `None` (no warm store)

## Endpoints
- `GET /health` → `HealthResponse`: pool status, device count, proxied count, per-device masked info. Secrets are masked (`_mask_proxy`); never returns raw proxy creds or identity internals.
- `POST /search` → `SearchResponse`. Request `SearchRequest` (`schemas.py`): `type` (keyword|hashtag|user), `query` (1–200 chars), `limit` (1–200, server-capped by `max_results_per_search`), `cursor` (>=0 offset), `fan_out` (1–32, defaults to config `default_fan_out`). Response: `query,type,device,count,cursor,next_cursor,has_more,elapsed_s,results[]`.

## Domain → HTTP error mapping (KEEP CENTRALIZED IN app.py)
All mapping lives in the `/search` handler's except blocks. Do not scatter it or leak secrets/internal detail into `detail`:

| Exception (`errors.py`)      | HTTP | detail                                                        |
|------------------------------|------|--------------------------------------------------------------|
| invalid query (`_bad`)       | 422  | `str(exc)`                                                    |
| `PoolExhausted` (daily cap)  | 429  | `exc.reason`                                                  |
| `PoolExhausted` (busy)       | 503  | `exc.reason`                                                  |
| `RateLimited`                | 429  | generic "slow down or add proxies" — no internals            |
| `SoftError` / `TransportError`| 502 | `f'TikTok request failed: {exc}'`                             |

## Response rule
`results[]` contains ONLY `mapping.py`-flattened records (`flatten_video`/`flatten_user`). Never return raw TikTok objects, `aweme_info`, or any identity/signer internals. `/health` masks all secrets.

## Adding an endpoint or request field
Thread it through every layer: add/extend the model in `schemas.py`, map it in `_to_query` (`api/app.py`) into a `SearchQuery` (`filters.py`), then use it when the client builds the request in `client.py` `build()`. Keep validation in Pydantic; keep error→HTTP translation in `app.py` only.

## Run locally + serve publicly
```bash
# from repo root
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000
# public tunnel
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate
```
`demo.html` is a static client for the API.

## curl examples
```bash
# health
curl -s http://127.0.0.1:8000/health | python -m json.tool

# keyword search
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":50,"fan_out":8}' | python -m json.tool

# user search
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"user","query":"nasa","limit":10}'

# hashtag search
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"hashtag","query":"climate","limit":30}'

# pagination: feed next_cursor back as cursor
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":50,"cursor":42}'
```

Cross-ref: `.claude/rules/api-service.md`, memory `common-changes-api.md`, `.claude/skills/pagination-harvesting/SKILL.md`.
