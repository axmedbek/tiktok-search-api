---
paths:
  - "mobile/tiktoksearch/api/**"
  - "mobile/api_signed.py"
---

# API / HTTP Service Standards

FastAPI service. App factory lives in `api/app.py`; schemas in `api/schemas.py`.

## Validation
- Every endpoint validates its input via a Pydantic schema in `schemas.py`. ❌ No raw request parsing in the handler.
- An invalid/empty query → `422` (Pydantic handles this at the boundary).

## Domain-error → HTTP mapping (central, in `app.py`)
- `SoftError` / `TransportError` → **502**
- `RateLimited` and daily-cap `PoolExhausted` → **429**
- busy `PoolExhausted` (no free client) → **503**
- Map exceptions in the central handler, not per-endpoint. ❌ Never leak the internal error text or any secret into the response `detail`.

## Responses
- Return only client-needed fields: the flattened records from `mapping.py`. ❌ Never return raw TikTok objects or identity internals (cookies, tokens, device ids).
- Health endpoint may expose device/identity status but MUST mask secrets (`first6…last4`).

## Config path resolution (keep this precedence)
`identities_path` resolves: env `TIKTOK_IDENTITIES_PATH` > config `identities_path` (relative to the config dir) > default.

## Running
- Serve via `api_signed.py --config <file>`. Default to `config_direct.yaml` for real results; `config_signed.yaml` is cold-legacy and returns empty by design.
