# Architecture

TikTok mobile-search-as-a-service. The project reproduces TikTok's signed mobile API well enough to return real, paginated keyword/user/hashtag search results without a phone or an official API — the core problem is **defeating ByteDance risk-control (`hit_shark`)**, not building CRUD.

## System context

```
client (curl / demo.html / caller)
        │  POST /search  { query, type, limit, page_token, fan_out }
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
signer: local  ──► MetasecSigner.for_v46()                   + capture_identity_addon.py)
        │             (in-process, no network, no quota)
        │  signer: rapid / hard local failure
        │          ──HTTP──► RapidAPI signer
        │  x-argus / x-gorgon / x-ladon / x-khronos
        ▼
TikTok search host (search19-normal-alisg.tiktokv.com)
        │  raw JSON (data[] mixed: videos + user cards + ads)
        ▼
mapping.py flatten_video / flatten_user ──► SearchPage(records, cursor, next_cursor,
                                                        has_more, endpoints, seen)
```

## Component responsibilities

| Module | Responsibility |
|--------|----------------|
| `api/app.py` | FastAPI factory. Resolves `identities_path` (env `TIKTOK_IDENTITIES_PATH` > config `identities_path:` relative-to-config > default). Wires IdentityStore into ClientPool. Endpoints: `POST /search`, `GET /health`. Decodes/mints `page_token` and pins the serving device. Maps domain errors → HTTP centrally: SoftError/TransportError → 502, RateLimited → 429, `PoolExhausted` by its `PoolCode` (`CAP` → 429, everything else → 503), and any token/query `ValueError` → 422. |
| `api/schemas.py` | Pydantic request/response models (`SearchRequest`, `SearchResponse`, `HealthResponse`). |
| `config.py` | Frozen `ClientConfig` / `PoolConfig` dataclasses. `from_mapping` (filters unknown keys, validates `signer:`), `with_overrides` (per-device merge), `resolved_signer()` (`local`/`rapid`/`legacy`; unset derives from `rapidapi_key` so pre-knob profiles behave unchanged). |
| `pool.py` | `ClientPool` — one `DeviceSlot` per identity (or per static device / synthetic). Round-robin proxy assignment. `acquire()` skips stale/exhausted slots and hot-reloads identities, or targets one slot by its stable `hmac(identity_key)` handle when a `page_token` pins it (never the positional `id{i}` label, which a file reorder would repoint at a different device). Daily per-device cap, UTC day roll. `run()` reports ok/empty back to the identity for health tracking. `run_merged()` fans out across slots and dedupes. |
| `identity_manager.py` | `IdentityStore` — loads warm identities from JSON, hot-reloads on mtime change, tracks per-identity health (N consecutive empty → `stale`; a new cookie/token resets health). This is the auto-refresh layer that fixes the "100/100 empty" failure. |
| `client.py` | `TikTokClient` — builds the search URL + query params, signs via the mode's signer (`_sign()`: local `MetasecSigner.for_v46` by default, `RapidSigner` for `rapid` or as a hard-failure fallback), GETs the search host, retries, and detects `hit_shark` (empty result → SoftError so it never leaks as a silent 200). Paginates two endpoints (`single/` + `search/item/`) merging deduped results, seeding each endpoint's cursor + `search_id` session from an incoming `page_token`. |
| `rapid_signer.py` | `RapidSigner` — calls the paid RapidAPI signer. Now the **fallback** (`signer: rapid`, or a hard local signing failure), and the only way to tell a stale sign key from stale credentials. |
| `signing.py` / `tiktok_signer/**` | Vendored pure-Python signer — **the default** (`signer: local`). `MetasecSigner.for_v46()` maps the `sign_*` v46 params onto the four fields `Metasec.sign` actually reads and attaches the warm identity; that mapping is what the old "frozen at v37" conclusion was missing. Plain construction stays the v32 cold `legacy` path. |
| `paging.py` | Opaque cross-request pagination token. HMAC-authenticated (`base64url(json).base64url(tag)`) under a per-process secret, so a caller cannot forge the device pin; carries per-endpoint `(path, cursor, search_id, has_more, started)`, an `hmac`-derived device handle (never a raw `device_id`), and a bounded window of recent record-id fingerprints that seeds `seen` across requests. Every bound is checked, and the tag is verified before any base64/JSON parse. Tokens die on restart. |
| `mapping.py` | `flatten_video` / `flatten_user` — turn raw TikTok aweme/user objects into stable, client-facing records. Skips non-video items (user cards, ads) in the mixed `data[]`. |
| `filters.py` | `SearchKind` / `SortType` / `PublishTime` enums, `SearchFilters` (→ flat v46 query params), `SearchQuery` (validated), `SearchPage` (result). |
| `errors.py` | Domain exceptions: `RateLimited`, `SoftError` (hit_shark / soft reject), `TransportError`, `PoolExhausted` — the last carrying a `PoolCode` (`CAP`/`BUSY`/`GONE`/`STALE`) so `app.py` maps status on an enum rather than a prose substring. |
| `capture_identity_addon.py` | mitmproxy addon (runs outside the app, alongside the emulator) — harvests fresh x-tt-token + sessionid + device fingerprint from the logged-in app and atomically rewrites `identities.json`. |

## The central problem: hit_shark (risk-control)

TikTok returns HTTP 200 with an **empty** `data[]` (and often a `search_nil_info` block) when it soft-rejects a request. The signature is valid — the rejection is about **identity trust**, not signing. Root causes, in order:
1. Expired warm `cookie` (sessionid) / `x_tt_token` — the #1 cause of "was working, now empty".
2. Cold / synthetic device_id (no warm fingerprint).
3. Version mismatch — the device activated as v46 but the request signs or *announces* another version (a `local` identity with no captured `user_agent` builds a UA quoting `version_code=320904` against a v46 signature; `signing.py` warns about exactly this), or a wrong endpoint/body shape.
4. Bad IP reputation (datacenter proxy, or many device_ids from one IP).

The architecture's answer: **warm, hot-reloadable identities with health tracking** (IdentityStore) + **residential proxy rotation** (pool) + **a capture loop** that refreshes credentials before they fully expire.

## Deployment (Docker)

`docker compose up` runs two services: `api` (built from `Dockerfile`, `python:3.11-slim`, non-root) on `127.0.0.1:8000`, and `ui` (`nginx:alpine`) serving `mobile/demo.html` on `127.0.0.1:8080`. The non-obvious parts:

- **Config and identities are bind-mounted, never copied.** `./mobile` is mounted read-only at `/app/config`, and `.dockerignore` excludes `mobile/*.yaml` + `mobile/identities.json*` from the build context, so no live `cookie` / `x_tt_token` / `rapidapi_key` reaches an image layer. The signer key comes from `RAPIDAPI_KEY` (`.env`), which `ClientConfig.from_mapping` lets win over the YAML value.
- **The whole directory is mounted, not the individual files.** `capture_identity_addon.py` writes `identities.json.tmp` then `os.replace()`s it; a *file* bind mount pins the host inode, so an atomic rename would be invisible inside the container forever. The directory mount is what makes mtime hot-reload work at all — and it also avoids Docker creating a stray host *directory* for the not-yet-existing `identities.json`.
- **Ports bind loopback only.** `/search` is unauthenticated, signs with a live warm identity and spends paid signer quota, and the services are `restart: unless-stopped`. Remote access is the tunnel's job (`demo.html` already accepts `?api=`).
- **`CMD` passes no `--config`.** `api_signed.py` builds a module-level `app` from `TTAPI_SIGNED_CONFIG`; `main()` only re-creates it when `args.config != _CONFIG`. Omitting the flag keeps them equal, so the pool is built once.
- **Code lands at `/app/tiktoksearch`** (not `/app/mobile/tiktoksearch`) because `api_signed.py` inserts its own directory onto `sys.path`.
- **A missing `identities.json` degrades, it does not raise.** `_resolve_identities_path` returns the env value without an existence check; `IdentityStore.reload` catches the `OSError`, logs `identities file … not found`, and `ClientPool._build_slots` falls back to the config's static `devices:` list (label `dev0`, not `id0`).
- **Startup announces misconfiguration loudly, and refuses the one state that cannot work at all.** `create_app` logs the resolved signer mode at INFO, and `ERROR` when the config path is absent or when a keyless profile resolves to `legacy` — the cold path, empty by design. That banner is boot-only (`/health` still reports `ok`) and silent under `signer: local`, where a missing key is normal. `signer: rapid` with no key is different in kind: `RapidSigner` raises on construction, so every client in the pool would fail to build. `create_app` validates the mode/key pair **before** the pool exists and raises a `ValueError` naming `signer: rapid` and `RAPIDAPI_KEY`, so the operator gets the cause instead of a bare error from inside pool setup — and no other signer is silently substituted, because `signer: rapid` is the stale-sign-key diagnostic.

## Signing: local by default, RapidAPI as the diagnostic

`signer:` decouples the signer from the direct-API path — `_direct` no longer means "has a RapidAPI key". `local` and `rapid` both take the direct path (`search_host`, `count=10`, `search_id`, hit_shark detection); only `legacy` is the old cold path.

The vendored signer works on v46: its `DEFAULT_SIGN_KEY` is still valid because `mssdk_ver_code 83952160` is shared between the 37.x and 46.x builds, and `app_version`/`sdk_version`/`sdk_version_code`/`license_id` are arguments to `Metasec.sign`, not baked constants. The old "frozen at v37" verdict measured a real failure but named the wrong cause: `signing.py` was passing the v32 defaults and attaching no warm identity.

**A stale sign key is silent, and looks exactly like stale credentials.** An MSSDK bump raises nothing — the signature stays well-formed and risk-control answers HTTP 200 with an empty `data[]`, so it surfaces as `hit_shark`, `IdentityStore` retires the identities, and the operator refreshes credentials that were never at fault. The discriminator is *every* identity failing at once shortly after a TikTok release: flip one profile to `signer: rapid`, re-run the same query, and compare. That single paid signature is the only thing that separates the two causes — which is why RapidAPI stays configured rather than deleted.

The fallback is deliberately narrow: only a signer-level `TransportError`, only in `local` mode, never on an empty result. Switching providers on empties is what `.claude/rules/lessons/anti-block.md` forbids.

## Data flow invariants

- `data[]` from `/aweme/v1/general/search/single/` is **mixed** — most items are videos (`type=1`, wrapped in `aweme_info`, has `aweme_id`), some are user cards / ads (no `aweme_id`). `client.py` `unwrap` and `mapping.py` skip non-video items. Never assume `data[0]` is a video.
- Pagination is a **session**, not an offset. `offset` = TikTok's own previous `cursor` (authoritative — dedup drops items, so a page can hold 15 records while the cursor is 20), and the request must echo `search_id` from `log_pb.impr_id`. A request at `offset > 0` *without* `search_id` gets `search_nil_item: empty_session` and no records — measured identically on the local and the RapidAPI signer, so it is session state, never signing or identity. An endpoint is **retired** (never re-queried) on exactly three conditions: TikTok's own `has_more=false`, a cursor that does not advance past the **previous** iteration's, or an allow-listed session tail. Separately, `_page_budget(limit, page_count)` — `ceil(limit/page_count)` plus slack, capped by `MAX_PAGES_PER_ENDPOINT` — bounds how much work *one request* may spend on an endpoint; exhausting that budget only ends the request and **leaves the endpoint resumable**. Conflating the two is what made a large `limit` self-defeating: a flat 12-page ceiling retired the endpoint at 120 raw items, so `limit=300` returned 34 records instead of 300.
- Callers resume with `page_token` (`paging.py`), never with a bare `cursor`. `next_cursor` is informational only.
- An empty result in direct mode is an **error condition** (`SoftError`) → HTTP 502 — **but only for a sessionless first page.** A request that carried a `search_id` is a continuation, and an empty reply there is the end of the stream: it returns `200 {count: 0, has_more: false, page_token: null}` in one signed request, and does not count against identity health. Without that split, following your own `page_token` to the end of any result set retired the sole warm identity in three requests (`DEFAULT_STALE_AFTER`) and 503'd every caller.
- The continuation tail is an **allow-list** (`empty_session`, `federation_empty`, or an empty page with no nil block at all) that **fails closed**: `hit_shark` or any nil this code does not recognise still raises `SoftError` → 502 and still reports the empty, so a session can never launder risk-control. An allow-listed tail is terminal for that endpoint regardless of the `has_more` the reply carries — trusting `has_more: true` on a reply already classified as terminal produced an unbounded chain of empty pages at one paid signature each.

## ADRs

| # | Title | Status |
|---|-------|--------|
| _(none yet)_ | | |
