# TikTok Mobile Search API

Signed, **phone-free** access to TikTok's mobile search backend
(`api*.tiktokv.com`). Requests are signed in pure Python
(`X-Argus` / `X-Gorgon` / `X-Ladon` / `X-Khronos`) — no phone, no login, no
browser. You get TikTok's full **mobile** JSON (canonical ids, author ids, view
counts, music, etc.), flattened to a stable schema, over a small FastAPI service.

- Keyword, hashtag, and user search
- Filters (sort by relevance/likes, recency window) for video searches
- Multi-device pool with budget-aware load balancing + optional per-device proxies
- Auto-generated interactive docs at `/docs`

> ⚠️ Accessing TikTok's private API violates its ToS and is a legal gray area.
> For research / authorized use on public data only. Review your obligations.

---

## 1. Requirements

- **Python 3.11+**
- macOS / Linux
- (optional) residential/mobile proxies for higher volume — see [Proxies](#proxies)

## 2. Setup

```bash
git clone https://github.com/axmedbek/tiktok-search-api.git
cd tiktok-search-api

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## 3. Run

```bash
python mobile/api_signed.py --config mobile/config_signed.yaml --port 8000
# or:
uvicorn mobile.api_signed:app --port 8000     # uses $TTAPI_SIGNED_CONFIG
```

The service starts with **5 synthetic device identities** by default (see config),
giving 1500 searches/day of capacity — enough to try everything immediately.

**Check it's up:**

```bash
curl localhost:8000/health
```

## 4. Documentation

| Where | What |
|-------|------|
| `http://localhost:8000/docs` | **Interactive** Swagger UI (try requests live) |
| `http://localhost:8000/openapi.json` | Raw OpenAPI schema |
| [`mobile/tiktoksearch/docs/API_REFERENCE.md`](mobile/tiktoksearch/docs/API_REFERENCE.md) | **Full written reference** — endpoints, filters, errors, config |
| `mobile/demo.html` | Browser demo UI (open in a browser while the server runs) |

---

## 5. Usage — HTTP examples

### Keyword search

```bash
curl -X POST localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{ "type": "keyword", "query": "climate change", "limit": 20 }'
```

### Keyword search with filters

```bash
curl -X POST localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{
    "type": "keyword",
    "query": "climate change",
    "limit": 20,
    "filters": { "sort_type": "1", "publish_time": "30" }
  }'
```

- `sort_type`: `"0"` relevance (default) · `"1"` most liked
- `publish_time`: `"0"` all · `"1"` 24h · `"7"` week · `"30"` month · `"90"` 3 months · `"180"` 6 months

### Hashtag search (the `#` is optional)

```bash
curl -X POST localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{ "type": "hashtag", "query": "bitcoin", "limit": 20 }'
```

### User search

```bash
curl -X POST localhost:8000/search \
  -H 'content-type: application/json' \
  -d '{ "type": "user", "query": "nasa", "limit": 10 }'
```

### Example response (video)

```json
{
  "query": "climate change",
  "type": "keyword",
  "device": "syn0",
  "count": 18,
  "elapsed_s": 2.4,
  "results": [
    {
      "id": "7658136483181776158",
      "description": "we only have one planet …",
      "create_time": "2026-07-03T03:21:04+00:00",
      "author_username": "cc.tyler1",
      "author_id": "7302270892632802346",
      "region_code": "US",
      "view_count": 5099527,
      "like_count": 1300435,
      "comment_count": 10252,
      "share_count": 75296,
      "hashtags": ["ClimateChange", "savetheplanet"],
      "music_id": "7658136491402464031",
      "music_title": "original sound - cc.tyler1",
      "duration": 31000,
      "source_term": "search:climate change"
    }
  ]
}
```

### Status codes

| Code | Meaning |
|------|---------|
| `200` | Success (`results` may be empty). |
| `422` | Bad body (invalid enum, empty query, or filters on a user search). |
| `429` | Daily cap reached on all devices, or TikTok rate-limited. |
| `502` | TikTok rejected/failed the request. |
| `503` | All devices busy past the acquire timeout. |

---

## 6. Usage — as a Python library

No HTTP server needed — import the package directly:

```python
import sys
sys.path.insert(0, "mobile")

from tiktoksearch import (
    TikTokClient, ClientConfig,
    SearchQuery, SearchKind, SearchFilters, SortType, PublishTime,
)

client = TikTokClient(ClientConfig())          # synthesizes a device id

query = SearchQuery(
    kind=SearchKind.KEYWORD,
    term="climate change",
    limit=20,
    filters=SearchFilters(
        sort_type=SortType.MOST_LIKED,
        publish_time=PublishTime.LAST_MONTH,
    ),
)

for record in client.search(query):
    print(record["author_username"], record["like_count"], record["view_count"])
```

For a load-balanced fleet, use the pool:

```python
from tiktoksearch import ClientPool, PoolConfig, SearchQuery, SearchKind

pool = ClientPool(PoolConfig.load_yaml("mobile/config_signed.yaml"))
device, results = pool.run(SearchQuery(kind=SearchKind.USER, term="nasa", limit=10))
print(f"served by {device}: {len(results)} users")
```

---

## 7. Configuration

Everything is driven by [`mobile/config_signed.yaml`](mobile/config_signed.yaml).
Key settings:

| Key | Default | Purpose |
|-----|---------|---------|
| `daily_request_cap_per_device` | `300` | Per-device, per-UTC-day search cap. |
| `synthetic_devices` | `5` | Auto-generated device identities (for dev/testing). |
| `devices` | `[]` | Explicit real device identities (steady use). |
| `proxies` | `[]` | Egress IPs, assigned round-robin. |
| `max_results_per_search` | `60` | Hard ceiling on `limit`. |
| `acquire_timeout_s` | `60` | Wait for a free device before `503`. |

Total daily capacity = **devices × `daily_request_cap_per_device`** (5 × 300 = 1500).

### Proxies

Without proxies, every device egresses through one host IP, and TikTok rate-limits
that IP. With proxies, each device gets its own IP. **Use residential/mobile
proxies** (datacenter IPs are blocked). Format `scheme://[user:pass@]host:port`:

```yaml
proxies:
  - "http://user:pass@gate.provider.com:7001"
  - "http://user:pass@gate.provider.com:7002"
```

---

## 8. Tests

```bash
cd mobile
../.venv/bin/python -m pytest tiktoksearch/tests -q
```

---

## 9. Project layout

```
requirements.txt
mobile/
├── api_signed.py            entrypoint (thin shim -> tiktoksearch.api.create_app)
├── config_signed.yaml       configuration
├── demo.html                browser demo UI
├── README_signed_api.md     signed-path notes
└── tiktoksearch/            the package
    ├── filters.py           SearchKind / SortType / PublishTime, SearchFilters, SearchQuery
    ├── mapping.py           raw TikTok JSON -> flat records
    ├── config.py            ClientConfig / PoolConfig
    ├── errors.py            exception hierarchy
    ├── signing.py           the only adapter over the vendored signer
    ├── client.py            signed HTTP, retries, pagination, filters
    ├── pool.py              device pool + load balancing + proxies
    ├── api/                 FastAPI app + pydantic schemas
    ├── docs/API_REFERENCE.md  full API reference
    ├── tests/               unit tests
    └── tiktok_signer/       vendored pure-Python request signer
```

---

## 10. Notes & limits

- **Synthetic vs real device ids:** synthetic (random) ids work but rate-limit
  sooner; some return empty results. For steady/high volume, use real device ids
  under `devices:` (must match the signer's app version — see the API reference).
- **Rate limits:** ~300–500 searches/day per device with an even cadence. Scale
  by adding devices **and** IPs (proxies), not by raising per-device speed.
