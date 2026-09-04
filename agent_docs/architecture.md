# Architecture

TikTok mobile-search-as-a-service. The project reproduces TikTok's signed mobile API well enough to return real, paginated keyword/user/hashtag search results without a phone or an official API — the core problem is **defeating ByteDance risk-control (`hit_shark`)**, not building CRUD.

## System context

```
client (curl / demo.html / caller)
        │  POST /search  { query, type, limit, cursor, fan_out }
        ▼
FastAPI app (api/app.py create_app)
        │
        ▼
ClientPool (pool.py) ──uses──► IdentityStore (identity_manager.py)
        │                              ▲
        │  acquire slot (warm identity, not stale, has budget)
        ▼                              │ hot-reload on mtime
TikTokClient (client.py)               │
        │  builds URL + query params    identities.json  ◄── capture loop
        ▼                                                    (emulator + mitmproxy
RapidSigner (rapid_signer.py) ──HTTP──► RapidAPI signer      + capture_identity_addon.py)
        │  x-argus / x-gorgon / x-ladon / x-khronos
        ▼
TikTok search host (search19-normal-alisg.tiktokv.com)
        │  raw JSON (data[] mixed: videos + user cards + ads)
        ▼
mapping.py flatten_video / flatten_user ──► SearchPage(records, cursor, next_cursor, has_more)
```

## Component responsibilities

| Module | Responsibility |
|--------|----------------|
| `api/app.py` | FastAPI factory. Resolves `identities_path` (env `TIKTOK_IDENTITIES_PATH` > config `identities_path:` relative-to-config > default). Wires IdentityStore into ClientPool. Endpoints: `POST /search`, `GET /health`. Maps domain errors → HTTP (SoftError/TransportError → 502, RateLimited/cap → 429, busy → 503). |
| `api/schemas.py` | Pydantic request/response models (`SearchRequest`, `SearchResponse`, `HealthResponse`). |
| `config.py` | Frozen `ClientConfig` / `PoolConfig` dataclasses. `from_mapping` (filters unknown keys), `with_overrides` (per-device merge). |
| `pool.py` | `ClientPool` — one `DeviceSlot` per identity (or per static device / synthetic). Round-robin proxy assignment. `acquire()` skips stale/exhausted slots and hot-reloads identities. Daily per-device cap, UTC day roll. `run()` reports ok/empty back to the identity for health tracking. `run_merged()` fans out across slots and dedupes. |
| `identity_manager.py` | `IdentityStore` — loads warm identities from JSON, hot-reloads on mtime change, tracks per-identity health (N consecutive empty → `stale`; a new cookie/token resets health). This is the auto-refresh layer that fixes the "100/100 empty" failure. |
| `client.py` | `TikTokClient` — builds the search URL + query params, signs via RapidSigner (direct mode) or MetasecSigner (legacy), GETs the search host, retries, and detects `hit_shark` (empty result → SoftError so it never leaks as a silent 200). Paginates two endpoints (`single/` + `search/item/`) merging deduped results. |
| `rapid_signer.py` | `RapidSigner` — calls the RapidAPI signer (`tiktanic` `/android/get_sign` or `working` `/sign`), returns x-argus/x-gorgon/x-ladon/x-khronos + assembled request headers. |
| `signing.py` / `tiktok_signer/**` | Vendored pure-Python signer (v37-era). Superseded by RapidSigner for v46; kept for the legacy `config_signed.yaml` path. |
| `mapping.py` | `flatten_video` / `flatten_user` — turn raw TikTok aweme/user objects into stable, client-facing records. Skips non-video items (user cards, ads) in the mixed `data[]`. |
| `filters.py` | `SearchKind` / `SortType` / `PublishTime` enums, `SearchFilters` (→ flat v46 query params), `SearchQuery` (validated), `SearchPage` (result). |
| `errors.py` | Domain exceptions: `RateLimited`, `SoftError` (hit_shark / soft reject), `TransportError`, `PoolExhausted`. |
| `capture_identity_addon.py` | mitmproxy addon (runs outside the app, alongside the emulator) — harvests fresh x-tt-token + sessionid + device fingerprint from the logged-in app and atomically rewrites `identities.json`. |

## The central problem: hit_shark (risk-control)

TikTok returns HTTP 200 with an **empty** `data[]` (and often a `search_nil_info` block) when it soft-rejects a request. The signature is valid — the rejection is about **identity trust**, not signing. Root causes, in order:
1. Expired warm `cookie` (sessionid) / `x_tt_token` — the #1 cause of "was working, now empty".
2. Cold / synthetic device_id (no warm fingerprint).
3. Version mismatch (device activated as v46 but signed as v37, or wrong endpoint/body shape).
4. Bad IP reputation (datacenter proxy, or many device_ids from one IP).

The architecture's answer: **warm, hot-reloadable identities with health tracking** (IdentityStore) + **residential proxy rotation** (pool) + **a capture loop** that refreshes credentials before they fully expire.

## Data flow invariants

- `data[]` from `/aweme/v1/general/search/single/` is **mixed** — most items are videos (`type=1`, wrapped in `aweme_info`, has `aweme_id`), some are user cards / ads (no `aweme_id`). `client.py` `unwrap` and `mapping.py` skip non-video items. Never assume `data[0]` is a video.
- Pagination: `offset` = previous cursor, echo `search_id` from `log_pb.impr_id`, `count=10` in direct mode. Stop on `has_more=false` or non-advancing cursor.
- An empty result in direct mode is an **error condition** (`SoftError`), not a valid empty page — surfaced as HTTP 502 so callers can distinguish "genuinely no results" from "shadow-blocked".

## ADRs

| # | Title | Status |
|---|-------|--------|
| _(none yet)_ | | |
