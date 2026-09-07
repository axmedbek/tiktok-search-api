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

### With Docker (API + demo UI)

```bash
cp .env.example .env          # RAPIDAPI_KEY is OPTIONAL under the default signer: local
docker compose up -d --build
docker compose logs -f api    # expect: "Signed search API up. N device(s) …"
```

| URL | What |
|-----|------|
| `http://localhost:8000` | the API (`/search`, `/health`, `/docs`) |
| `http://localhost:8080` | the demo UI (`mobile/demo.html`, served by nginx) |

Both ports bind **loopback only**. `/search` has no authentication, signs with a
live warm identity, and the services are
`restart: unless-stopped` — so they are deliberately not published on all
interfaces. For remote access put a tunnel in front:
`cloudflared tunnel --url http://127.0.0.1:8000`, then point the UI at it with
`http://localhost:8080/?api=https://<tunnel-host>`.

Config and warm identities are **bind-mounted read-only** from `./mobile` to
`/app/config` — nothing secret is baked into the image (see `.dockerignore`).
The whole directory is mounted, not individual files, so the capture loop's
atomic `os.replace()` of `identities.json` stays visible to the container and
hot-reload keeps working.

> The default profile uses `signer: local` — the vendored signer signs v46
> in-process at **zero** signer quota, so an unset `RAPIDAPI_KEY` is the normal
> working state. RapidAPI is kept for two jobs: re-signing a hard local signing
> failure, and diagnosing a stale local sign key (flip a profile to
> `signer: rapid` and re-run the same query). Startup logs the resolved mode —
> check `docker compose logs api` first if results surprise you.

### Natively

```bash
python mobile/api_signed.py --config mobile/config_direct.yaml --port 8000
# or:
uvicorn mobile.api_signed:app --port 8000     # uses $TTAPI_SIGNED_CONFIG
```

`config_direct.yaml` serves from its warm device identity (or from
`identities.json` when the capture loop has written one), with a per-device cap
of 500 searches/day. `config_signed.yaml` is the cold `legacy` profile and
returns empty by design — don't use it for real results.

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

### Pagination — pass `page_token` back, never `next_cursor`

TikTok's search is a **session**: a request at a non-zero offset must echo the
previous reply's `search_id`, or the API answers `empty_session` and returns
nothing. So resend the response's `page_token`; feeding `next_cursor` back as
`cursor` is a sessionless request and comes back `502 empty_session`.

```bash
# page 1
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{ "type": "keyword", "query": "cats", "limit": 20 }'
# -> { "count": 20, "next_cursor": 20, "has_more": true, "page_token": "eyJ2Ijox…" }

# page 2 — the token is the only thing that changes
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{ "type": "keyword", "query": "cats", "limit": 20, "page_token": "eyJ2Ijox…" }'
```

Stop when `page_token` comes back `null`; the end of a stream is a normal `200`
with `count: 0`, not an error. `next_cursor` is TikTok's own cursor and is
informational only. `cursor` is still accepted for backwards compatibility.

Notes: the token pins the device that served the previous page, so an explicit
`fan_out > 1` alongside it is a `422`. Tokens are authenticated and do not
survive a server restart (a stale one is a `422`, not a resumable session).
Cross-page dedup covers a bounded window of recent records, so a repeat is
possible only far deeper than a stream normally goes.

### Results per search (fan-out)

TikTok returns ~10 results per call per device. `fan_out` queries several
devices in parallel and merges/dedupes them, at one daily-cap unit per device.
`config_direct.yaml` ships `default_fan_out: 1`, because a merged page spans
several devices and therefore cannot mint a `page_token` — fan-out and
pagination are alternatives, not companions.

```bash
# deeper results from one device: paginate with page_token (above)
curl -X POST localhost:8000/search -d '{"type":"keyword","query":"cats","limit":60}'

# wider results in one call: fan out across the pool (no page_token)
curl -X POST localhost:8000/search -d '{"type":"keyword","query":"cats","fan_out":8}'
```

`fan_out` is capped at the pool size, so it only helps once the pool holds
several warm identities. Returns diminish past ~6 as popular queries repeat
across devices.

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
  "device": "dev0",
  "count": 18,
  "cursor": 0,
  "next_cursor": 20,
  "has_more": true,
  "page_token": "eyJ2IjoxLCJxIjoi…",
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
| `422` | Bad body (invalid enum, empty query, filters on a user search), or a rejected `page_token` (malformed, tampered, minted for another query, from a previous server run, or combined with a non-zero `cursor` / an explicit `fan_out > 1`). |
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

page = client.search(query)                    # -> SearchPage
for record in page.records:
    print(record["author_username"], record["like_count"], record["view_count"])
print("next_cursor:", page.next_cursor, "has_more:", page.has_more)
```

For a load-balanced fleet, use the pool:

```python
from tiktoksearch import ClientPool, PoolConfig, SearchQuery, SearchKind

pool = ClientPool(PoolConfig.load_yaml("mobile/config_signed.yaml"))
device, page = pool.run(SearchQuery(kind=SearchKind.USER, term="nasa", limit=10))
print(f"served by {device}: {len(page.records)} users")
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
Dockerfile                   API image (python:3.11-slim, non-root)
docker-compose.yml           api (:8000) + ui (nginx, :8080), loopback-bound
.dockerignore                keeps configs/identities out of image layers
.env.example                 RAPIDAPI_KEY template
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
