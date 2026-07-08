# TikTok Signed Search API (no phone, direct API)

A **synchronous search API** that signs requests to TikTok's mobile backend
(`api*.tiktokv.com`) using a **pure-Python reimplementation** of the app's
client-side signing (`X-Argus` / `X-Gorgon` / `X-Ladon` / `X-Khronos`). No phone,
no Appium, no browser — a search returns **full JSON in ~1–3 seconds**:
canonical `aweme_id`, `author_id`, view counts, music, hashtags, everything the
UI-scraping path could not expose.

This is the fast path. The Appium path (`README_api.md`) still exists as a
lower-throughput, UI-based fallback, but the signed path is what you want for
200–300 searches/day.

> ⚠️ Same legal note as the rest of the project: hitting TikTok's private API
> without authorization violates its ToS and is a legal gray area. Public data,
> research use, review your IRB/GDPR/CFAA obligations. You are responsible.

---

## How it works

```
POST /search → FastAPI → TikTokClient → Metasec.sign() → GET api.tiktokv.com/.../search → JSON
```

```
tiktoksearch/            the package (clean, SOLID, team-shareable)
  filters.py             SearchKind/SortType/PublishTime enums, SearchFilters, SearchQuery
  mapping.py             raw TikTok JSON -> flat records (pure functions)
  config.py              ClientConfig / PoolConfig (frozen, YAML-loaded)
  errors.py              TikTokSearchError hierarchy
  signing.py             MetasecSigner — the ONLY adapter over the vendored signer
  client.py              TikTokClient — signed HTTP, host rotation, retries, pagination, FILTERS
  pool.py                DeviceSlot / ClientPool — budget-aware LRU load balancing + proxies
  tiktok_signer/         vendored pure-Python signer (from armxe/tiktok-api, unchanged)
  api/app.py             create_app() factory: POST /search, GET /health, /docs
  api/schemas.py         pydantic models (validation + auto-docs)
  docs/API_REFERENCE.md  full written API reference  ← read this
api_signed.py            thin entrypoint shim -> tiktoksearch.api.create_app
config_signed.yaml       devices, proxies, app version, rate/budget knobs
```

**Full API reference:** [tiktoksearch/docs/API_REFERENCE.md](tiktoksearch/docs/API_REFERENCE.md)
· live interactive docs at **`GET /docs`** when running.

### Search filters (new)

Video searches (`keyword`/`hashtag`) accept optional filters:

```bash
curl -X POST localhost:8000/search -H 'content-type: application/json' -d '{
  "type":"keyword","query":"climate change","limit":20,
  "filters":{"sort_type":"1","publish_time":"30"}
}'
```

- `sort_type`: `0` relevance · `1` most liked
- `publish_time`: `0` all · `1` 24h · `7` week · `30` month · `90` 3mo · `180` 6mo

Filters on a `user` search return 422. See the API reference for details.

## Multi-device pool & load balancing

Each request is served by ONE device identity (`device_id` + `iid`). A single
identity rate-limits quickly, so run several: the pool spreads `/search` across
all devices, picking the **least-recently-used device that still has budget**
(even load), one in-flight request per device. So **N devices = N concurrent
searches**, and total daily capacity = `devices × daily_request_cap_per_device`
(e.g. 5 × 300 = **1500/day**). A device that hits its daily cap is skipped until
the UTC day rolls over; when every device is capped, `/search` returns 429.

Configure devices two ways (combinable) in `config_signed.yaml`:

```yaml
synthetic_devices: 5        # N random identities — for TESTING
devices:                    # explicit REAL identities (from recon captures)
  - device_id: "7100000000000000001"
    iid:       "7100000000000000101"
  - device_id: "7100000000000000002"
    iid:       "7100000000000000102"
    app_version: "33.1.0"   # optional per-device override
    version_code: "330100"
```

`GET /health` shows per-device `used_today` / `remaining_today` / `busy` and the
pool's `total_daily_capacity`. Each `/search` response includes which `device`
served it.

> ⚠️ Synthetic (random) ids work for exercising the API, but TikTok returns
> **empty or thin results** for some unregistered devices and rate-limits them
> sooner. For real data at volume, fill `devices:` with real captured ids.

The signer needs `pycryptodome` (AES) and `gmssl` (SM3):

```bash
cd /Users/axmedbek/PhpstormProjects/tiktok-searcher
source .venv/bin/activate
pip install requests pycryptodome gmssl     # or: pip install -r requirements.txt
```

---

## Run

```bash
python mobile/api_signed.py --config mobile/config_signed.yaml --port 8000
# or:  uvicorn mobile.api_signed:app --port 8000
```

```bash
curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"climate change","limit":20}' | jq
```

---

## Endpoints

### `POST /search`

```jsonc
{ "type": "keyword",   // "keyword" | "hashtag" | "user"
  "query": "climate change",
  "limit": 20 }        // 1..200, clamped to config max_results_per_search
```

Video response (`keyword` / `hashtag`) — one result:

```jsonc
{ "id": "7658136483181776158", "description": "we only have one planet…",
  "create_time": "2026-07-01T12:41:04+00:00",
  "author_username": "cc.tyler1", "author_id": "7302270892632802346",
  "view_count": 4336396, "like_count": 1098717,
  "comment_count": 9218, "share_count": 66019,
  "hashtags": ["ClimateChange","savetheplanet","fyp"],
  "music_id": "7658136491402464031", "music_title": "original sound - cc.tyler1",
  "duration": 21, "region_code": "GB", "source_term": "search:climate change" }
```

User response (`type: "user"`):

```jsonc
{ "type": "user", "id": "6826090951188448262", "username": "nasablueberry1",
  "display_name": "Alyssa Carson", "follower_count": 397637,
  "following_count": 12, "aweme_count": 88, "verified": true,
  "region_code": "US", "user_id": "6826090951188448262",
  "source_term": "user:nasa" }
```

Codes: `200` ok · `422` bad type/body · `429` daily cap hit **or** TikTok
rate-limited · `502` TikTok request failed · `503` server busy past
`acquire_timeout_s`.

### `GET /health`

```jsonc
{ "status":"ok","daily_cap":300,"used_today":12,"remaining_today":288,
  "device_id":"7156614170174012825","app_version":"32.9.4" }
```

---

## Device identity — important for steady use

Signing needs a `device_id` + `iid` (install id). Left blank in
`config_signed.yaml`, the client **synthesizes random ids at startup** — fine for
light testing (verified working), but a fresh unregistered id gets
**rate-limited sooner** and TikTok's `/feed/` already 429s quickly.

For steady 200–300/day, seed a **real** identity captured from your own phone:

1. Run the recon capture (`recon/run_recon.sh`) and browse TikTok on a phone
   routed through the proxy.
2. In `recon/out/endpoints.jsonl`, read the query params of any
   `api*.tiktokv.com` request — copy `device_id`, `iid`, `version_code`,
   `version_name`, and the `User-Agent`.
3. Paste `device_id` / `iid` into `config_signed.yaml` and match `app_version` /
   `version_code` to what your capture shows.

Matching a real, warmed device identity is the single biggest factor in staying
un-rate-limited.

## Tuning (`config_signed.yaml`)

- `daily_request_cap` — per-UTC-day `/search` cap (your 200–300 target).
- `max_concurrency` — in-flight searches; keep at 1–2 to avoid rate-limits.
- `max_results_per_search` — hard ceiling on `limit`.
- `request_timeout_s` / `retries` — per-request timeout and host-rotation retries.
- `api_hosts` — the `api*.tiktokv.com` hosts rotated on retry.

## When results stop coming back

If searches start returning 429 or empty: the signing key or app-version fields
have drifted, or the device id is burned. Fixes, in order: seed a fresh real
`device_id`/`iid` + matching `app_version`/`version_code` from a new recon
capture; if signatures themselves are rejected (not just rate-limited), the
signer in `tiktok_signer/` needs updating against the upstream
[`armxe/tiktok-api`](https://github.com/armxe/tiktok-api) `Mobile/` package.

## Credit

`tiktok_signer/` is vendored from the open-source
[`armxe/tiktok-api`](https://github.com/armxe/tiktok-api) `Mobile/` package.
