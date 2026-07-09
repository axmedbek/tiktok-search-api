# TikTok Mobile Search API — Reference

Signed, phone-free access to TikTok's **mobile** search backend
(`api*.tiktokv.com`). Requests are signed in pure Python
(`X-Argus` / `X-Gorgon` / `X-Ladon` / `X-Khronos`) — no phone, no login, no
browser. Responses are TikTok's full mobile JSON, flattened to a stable schema.

- **Interactive docs (live):** `GET /docs` (Swagger UI) · `GET /openapi.json`
- **Base URL (default):** `http://localhost:8000`

> ⚠️ Accessing TikTok's private API violates its ToS and is a legal gray area.
> For research/authorized use on public data only. Review your obligations.

---

## Architecture

```
tiktoksearch/
├── filters.py    SearchKind / SortType / PublishTime enums, SearchFilters, SearchQuery
├── mapping.py    raw TikTok JSON -> flat records (pure functions)
├── config.py     ClientConfig / PoolConfig (frozen, YAML-loaded)
├── errors.py     TikTokSearchError hierarchy
├── signing.py    MetasecSigner — the ONLY adapter over the vendored signer
├── client.py     TikTokClient — signed HTTP, host rotation, retries, pagination
├── pool.py       DeviceSlot / ClientPool — budget-aware LRU load balancing
├── tiktok_signer/  vendored pure-Python signer (unchanged)
└── api/
    ├── app.py    create_app() factory, routes, error->HTTP mapping
    └── schemas.py  pydantic request/response models (power the auto-docs)
```

Layering is one-directional: `api → pool → client → {signing, mapping}`, all
reading `config` / `filters` / `errors`. Each third-party seam is isolated:
`signing.py` is the only importer of the signer, `client.py` the only importer
of `requests`, `api/` the only importer of `fastapi`.

---

## Run

```bash
cd /Users/axmedbek/PhpstormProjects/tiktok-searcher
source .venv/bin/activate
pip install -r requirements.txt
python mobile/api_signed.py --config mobile/config_signed.yaml --port 8000
# or:  uvicorn mobile.api_signed:app --port 8000   (uses $TTAPI_SIGNED_CONFIG)
```

Use as a library (no HTTP):

```python
import sys; sys.path.insert(0, "mobile")
from dataclasses import replace
from tiktoksearch import TikTokClient, ClientConfig, SearchQuery, SearchKind, \
    SearchFilters, SortType, PublishTime

client = TikTokClient(ClientConfig())          # synthesizes a device id
query = SearchQuery(
    kind=SearchKind.KEYWORD, term="climate change", limit=20, cursor=0,
    filters=SearchFilters(sort_type=SortType.MOST_LIKED,
                          publish_time=PublishTime.LAST_MONTH),
)
page = client.search(query)          # -> SearchPage
records = page.records               # list[dict]
# next page:
if page.has_more:
    next_page = client.search(replace(query, cursor=page.next_cursor))
```

---

## `POST /search`

Run a keyword, hashtag, or user search.

### Request body

| Field     | Type    | Req | Default | Notes |
|-----------|---------|-----|---------|-------|
| `type`    | enum    | ✓   | —       | `keyword` \| `hashtag` \| `user` |
| `query`   | string  | ✓   | —       | 1–200 chars. For `hashtag`, the `#` is optional. |
| `limit`   | int     |     | `30`    | Max results **per page**; 1–200, capped by `max_results_per_search` (default 60). |
| `cursor`  | int     |     | `0`     | Pagination offset. Pass a prior response's `next_cursor` to fetch the next page; start at `0`. |
| `fan_out` | int     |     | server  | Query N devices in parallel and merge+dedupe. Omit to use the server `default_fan_out` (6 → ~30+ results). `1` = one device (~10, cheapest). Each unit spends one device's daily budget. |
| `filters` | object  |     | `null`  | Video searches only (`keyword`/`hashtag`). See below. |

### Results per search (fan-out)

TikTok gives each device — especially synthetic ids — a **shallow window** (~10
results, then `has_more=false`). To return more per call, the API queries several
devices in parallel and **merges + dedupes** their results. This is controlled by
`fan_out` (per request) or `default_fan_out` (server config, default `6`).

| `fan_out` | Typical merged results | Budget cost |
|---|---|---|
| `1` | ~10 | 1 device |
| `6` (default) | **~30–40** | 6 devices |
| `15` | ~50 | 15 devices (diminishing returns) |

A plain request (no `fan_out`) already returns ~30+. Returns diminish past ~6
because popular queries surface the same top results across devices. **Real
device ids return hundreds each** — with them, set `default_fan_out: 1`.

### Filters (video searches only)

Sent to TikTok as its native `filter_selected` blob with `is_filter_search=1`.
TikTok applies them **server-side, best-effort** — results approximate the
requested window, they are not a hard guarantee.

| `filters.` field | Values | Meaning |
|---|---|---|
| `sort_type`    | `"0"` | Relevance (default) |
|                | `"1"` | Most liked |
| `publish_time` | `"0"` | All time (default) |
|                | `"1"` | Last 24 hours |
|                | `"7"` | Last week |
|                | `"30"`| Last month |
|                | `"90"`| Last 3 months |
|                | `"180"`| Last 6 months |

Supplying `filters` on a `user` search returns **422** (not supported).

### Examples

```bash
# keyword, most-liked, last month
curl -X POST localhost:8000/search -H 'content-type: application/json' -d '{
  "type": "keyword", "query": "climate change", "limit": 20,
  "filters": { "sort_type": "1", "publish_time": "30" }
}'

# hashtag (# optional)
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{ "type": "hashtag", "query": "bitcoin", "limit": 20 }'

# user
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{ "type": "user", "query": "nasa", "limit": 10 }'

# page 2: pass the previous response's next_cursor
curl -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{ "type": "keyword", "query": "climate change", "limit": 20, "cursor": 20 }'
```

### Pagination

Responses are cursor-paged. Start with `cursor: 0` (or omit it); each response
returns `next_cursor` and `has_more`. To fetch the next page, resend the same
request with `cursor` set to the previous `next_cursor`. Stop when
`has_more` is `false` (then `next_cursor` is `null`).

```python
cursor, all_results = 0, []
while True:
    r = requests.post(url, json={"type": "keyword", "query": "cats",
                                 "limit": 20, "cursor": cursor}).json()
    all_results += r["results"]
    if not r["has_more"]:
        break
    cursor = r["next_cursor"]
```

> **Depth note.** How deep pagination goes depends on the device identity.
> Synthetic ids typically get a shallow window (TikTok's anti-scrape guard) — the
> API returns the correct `next_cursor`/`has_more`, but a follow-up page may come
> back empty. Real, warmed device ids (and per-device proxies) unlock deeper
> paging automatically, with no code change.

### Response

```jsonc
{
  "query": "climate change",
  "type": "keyword",
  "device": "syn0",          // which pool device served it
  "count": 18,
  "cursor": 0,               // the cursor this page started from
  "next_cursor": 20,         // pass as `cursor` for the next page (null when done)
  "has_more": true,          // more results available?
  "elapsed_s": 2.4,
  "results": [ /* video or user records */ ]
}
```

**Video record** (`keyword` / `hashtag`):

```jsonc
{
  "id": "7658136483181776158",       // canonical aweme id
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
```

**User record** (`type: "user"`):

```jsonc
{
  "type": "user",
  "id": "6826090951188448262",
  "username": "nasablueberry1",
  "display_name": "Alyssa Carson",
  "follower_count": 397637,
  "following_count": 12,
  "aweme_count": 88,
  "signature": "…",
  "region_code": "US",
  "verified": true,
  "user_id": "6826090951188448262",
  "source_term": "user:nasa"
}
```

### Status codes

| Code | Meaning |
|------|---------|
| `200` | Success. `results` may be empty if the feed ran dry. |
| `422` | Invalid body (bad enum, empty query, or filters on a user search). |
| `429` | Daily cap reached on all devices, **or** TikTok rate-limited the request. |
| `502` | TikTok rejected/failed the request (soft error or transport failure). |
| `503` | All devices busy past `acquire_timeout_s`. |

---

## `GET /health`

Fleet + per-device budget, for monitoring.

```jsonc
{
  "status": "ok",
  "device_count": 5,
  "idle": 4,
  "total_daily_capacity": 1500,
  "capacity_remaining_today": 1487,
  "devices": [
    { "label": "syn0", "device_id": "71…", "iid": "71…", "proxy": null,
      "used_today": 13, "daily_cap": 300, "remaining_today": 287, "busy": true }
  ]
}
```

Proxy credentials are masked (`host:port` only).

---

## Configuration (`config_signed.yaml`)

| Key | Default | Purpose |
|---|---|---|
| `daily_request_cap_per_device` | `300` | Per-device, per-UTC-day search cap. Total = devices × this. |
| `acquire_timeout_s` | `60` | Wait for a free device before `503`. |
| `max_results_per_search` | `60` | Hard ceiling on `limit`. |
| `request_timeout_s` | `20` | Per-request timeout to TikTok. |
| `retries` | `2` | Transient-error retries (rotates `api_hosts`). |
| `synthetic_devices` | `5` | Auto-generated random device ids (testing). |
| `devices` | `[]` | Explicit real device ids (steady use). |
| `proxies` | `[]` | Egress IPs, assigned round-robin (see below). |
| `app_version` … `channel` | — | App fingerprint; must match the signer's SDK. |
| `api_hosts` | 3 hosts | `api*.tiktokv.com` hosts rotated on retry. |

### Devices & load balancing

The pool routes each `/search` to the **least-recently-used device with daily
budget left**, one in-flight request per device. So N devices give N concurrent
searches, evenly spread; a capped device is skipped until the UTC day rolls over.

- `synthetic_devices: N` — random ids; work but rate-limit sooner. Good for dev.
- `devices:` — explicit real ids captured from a device; steadier. Each entry
  may override any app-version field:
  ```yaml
  devices:
    - device_id: "7100000000000000001"
      iid:       "7100000000000000101"
      proxy:     "http://user:pass@host:7001"   # optional per-device
  ```

### Proxies (IP isolation)

The single biggest anti-block lever. Without proxies, every device egresses
through one host IP — TikTok sees many device ids on one IP and rate-limits it.
With proxies, each device gets its own IP. Assigned round-robin; a device with
its own `proxy:` keeps it. **Use residential/mobile proxies** (datacenter IPs
are routinely blocked). Format `scheme://[user:pass@]host:port` (http/https/socks5).

```yaml
proxies:
  - "http://user:pass@gate.provider.com:7001"
  - "http://user:pass@gate.provider.com:7002"
```

---

## Rate limits & blocking (what to expect)

- **device_id block** (light): one id over-used → that id starts returning
  403/empty. Fix: rotate to a fresh id (synthetic ids do this automatically).
- **IP rate-limit `429`** (the main risk on one IP): too many/too-fast requests
  from one IP → temporary throttle affecting all devices on it. Fix: pace
  requests evenly and/or use proxies.
- **Account block:** N/A — this never logs in.

Practical safe budget per device with an even cadence: **~300–500/day**. Scale
by adding devices **and** IPs (proxies), not by raising per-device speed.

---

## Extending

- **New filter:** add the enum in `filters.py`, wire it in
  `SearchFilters.to_query_params()`, expose it in `api/schemas.py:FiltersIn`.
  Nothing else changes.
- **New search kind / endpoint:** add a `SearchKind`, a `_search_*` method in
  `client.py` reusing `_paginate`, and a `flatten_*` in `mapping.py`.
- **Real device ids:** capture `device_id` + `iid` from a device and put them
  under `devices:`. They must match the signer's app version (see below).

## Maintenance note — signer version

The vendored signer targets a specific TikTok app version (`app_version` /
`sdk_version` in config). Synthetic ids work across versions; **real** device
ids must be registered on the *same* app version the signer targets, or TikTok
returns 403. If TikTok updates its signing SDK, refresh `tiktok_signer/` from
upstream ([`armxe/tiktok-api`](https://github.com/armxe/tiktok-api)) and the
version fields together.
