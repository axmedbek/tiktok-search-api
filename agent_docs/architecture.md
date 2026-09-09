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
| `api/app.py` | FastAPI factory. Resolves `identities_path` (env `TIKTOK_IDENTITIES_PATH` > config `identities_path:` relative-to-config > default). Wires IdentityStore into ClientPool. Endpoints: `POST /search`, `POST /profile`, `POST /user/posts`, `GET /health`. One `_domain_errors()` context manager is THE domain→HTTP map for every handler — a hand-copied per-handler map is how a class silently gets dropped (`NotFound` was). Decodes/mints `page_token` and pins the serving device. Maps domain errors → HTTP centrally: SoftError/TransportError → 502, RateLimited → 429, `PoolExhausted` by its `PoolCode` (`CAP` → 429, everything else → 503), and any token/query `ValueError` → 422. |
| `api/schemas.py` | Pydantic request/response models (`SearchRequest`, `SearchResponse`, `ProfileRequest/Response`, `UserPostsRequest/Response`, `HealthResponse`). A handle reaches a signed TikTok URL as the search keyword, so it is length- and charset-bounded here (24 chars, `[A-Za-z0-9._]` — hyphens are correctly rejected, TikTok handles have none) with one leading `@` stripped in a `mode='before'` validator. A field carrying an interim-contract fact is declared WITHOUT a Pydantic default, or it is omitted from OpenAPI's `required` list and a generated client types it optional — which is the footnote status such a field exists to avoid. |
| `config.py` | Frozen `ClientConfig` / `PoolConfig` dataclasses. `from_mapping` (filters unknown keys, validates `signer:`), `with_overrides` (per-device merge), `resolved_signer()` (`local`/`rapid`/`legacy`; unset derives from `rapidapi_key` so pre-knob profiles behave unchanged). |
| `pool.py` | `run_call(fn, *, handle=None)` is the seam every pooled call goes through; `fn` returns `CallOutcome(result, verdict)` with an explicit `HealthVerdict` (`OK`/`EMPTY`/`NEUTRAL`, no default). `_search_verdict` and `posts_verdict` sit side by side so the two identity-health rules are read against each other. `ClientPool` — one `DeviceSlot` per identity (or per static device / synthetic). Round-robin proxy assignment. `acquire()` skips stale/exhausted slots and hot-reloads identities, or targets one slot by its stable `hmac(identity_key)` handle when a `page_token` pins it (never the positional `id{i}` label, which a file reorder would repoint at a different device). Daily per-device cap, UTC day roll. `run()` reports ok/empty back to the identity for health tracking. `run_merged()` fans out across slots and dedupes. |
| `identity_manager.py` | `IdentityStore` — loads warm identities from JSON, hot-reloads on mtime change, tracks per-identity health (N consecutive empty → `stale`; a new cookie/token resets health). This is the auto-refresh layer that fixes the "100/100 empty" failure. |
| `client.py` | `TikTokClient` — builds the search URL + query params, signs via the mode's signer (`_sign()`: local `MetasecSigner.for_v46` by default, `RapidSigner` for `rapid` or as a hard-failure fallback), GETs the search host, retries, and detects `hit_shark` (empty result → SoftError so it never leaks as a silent 200). Paginates two endpoints (`single/` + `search/item/`) merging deduped results, seeding each endpoint's cursor + `search_id` session from an incoming `page_token`. Emptiness is classified per endpoint by a `PayloadShape` handed to `_get_signed` — see § Emptiness is per-endpoint. |
| `rapid_signer.py` | `RapidSigner` — calls the paid RapidAPI signer. Now the **fallback** (`signer: rapid`, or a hard local signing failure), and the only way to tell a stale sign key from stale credentials. |
| `signing.py` / `tiktok_signer/**` | Vendored pure-Python signer — **the default** (`signer: local`). `MetasecSigner.for_v46()` maps the `sign_*` v46 params onto the four fields `Metasec.sign` actually reads and attaches the warm identity; that mapping is what the old "frozen at v37" conclusion was missing. Plain construction stays the v32 cold `legacy` path. |
| `paging.py` | Opaque cross-request pagination token. HMAC-authenticated (`base64url(json).base64url(tag)`) under a per-process secret, so a caller cannot forge the device pin; carries per-endpoint `(path, cursor, search_id, has_more, started)`, an `hmac`-derived device handle (never a raw `device_id`), and a bounded window of recent record-id fingerprints that seeds `seen` across requests. The tag is verified before any base64/JSON parse. The cursor bound is **per-path** via `cursor_bound(path)` and is enforced on `encode` as well as `decode`; the remaining field bounds are decode-only, held at ingest by their producers. Tokens die on restart. |
| `mapping.py` | `flatten_video` / `flatten_user` / `flatten_profile` — turn raw TikTok aweme/user objects into stable, client-facing records. Skips non-video items (user cards, ads) in the mixed `data[]`. Pure and dependency-free: no logger, no I/O, no reach into `client`/`pool`/`config`. A payload it cannot use is rejected by returning `None`; it never tries to tell "no such user" from `hit_shark`, because it holds no session or identity context to do so — that split lives in `client.py`. Coercion goes through `to_int` / `_str_or_none` / `_flag` / `_verified`, never inline. |
| `filters.py` | `SearchKind` / `SortType` / `PublishTime` enums, `SearchFilters` (→ flat v46 query params), `SearchQuery` (validated), `SearchPage` (result). |
| `errors.py` | Domain exceptions: `RateLimited`, `SoftError` (hit_shark / soft reject), `TransportError`, `NotFound` (upstream says no such user → 404), `PoolExhausted` — the last carrying a `PoolCode` (`CAP`/`BUSY`/`GONE`/`STALE`) so `app.py` maps status on an enum rather than a prose substring. `NotFound` is deliberately **not** a `SoftError` subclass: `pool.run` penalises the identity only in `except SoftError`, so staying outside that hierarchy is what makes "a deleted user costs the identity nothing" structurally true rather than a convention. |
| `capture_identity_addon.py` | mitmproxy addon (runs outside the app, alongside the emulator) — harvests fresh x-tt-token + sessionid + device fingerprint from the logged-in app and atomically rewrites `identities.json`. |

## The central problem: hit_shark (risk-control)

TikTok returns HTTP 200 with an **empty** `data[]` (and often a `search_nil_info` block) when it soft-rejects a request. The signature is valid — the rejection is about **identity trust**, not signing. Root causes, in order:
1. Expired warm `cookie` (sessionid) / `x_tt_token` — the #1 cause of "was working, now empty".
2. Cold / synthetic device_id (no warm fingerprint).
3. Version mismatch — the device activated as v46 but the request signs or *announces* another version (a `local` identity with no captured `user_agent` builds a UA quoting `version_code=320904` against a v46 signature; `signing.py` warns about exactly this), or a wrong endpoint/body shape.
4. Bad IP reputation (datacenter proxy, or many device_ids from one IP).

The architecture's answer: **warm, hot-reloadable identities with health tracking** (IdentityStore) + **residential proxy rotation** (pool) + **a capture loop** that refreshes credentials before they fully expire.

## The user-scoped endpoints, and why they are served from search

`POST /profile` and `POST /user/posts` do **not** call the upstream user endpoints — those are live handlers that reject this client's param set (see § data flow invariants). Both are served from a search reply instead:

- **`/profile`** flattens the `user_list` node that the handle resolve already fetched, so it costs **one** signed request. `signature` and `region_code` are always `null` on this path: the node does not carry them (measured null across 15 users, two independent searches).
- **`/user/posts`** keyword-searches the handle and keeps records whose author matches. It is **not chronological** (search orders by relevance) and **not complete** (it returns what search surfaces — measured ~50-60 of an account's 5225 posts, about 1%). `source` and `complete` are REQUIRED response fields so a caller can detect that programmatically.

Three non-obvious consequences:

- **The author filter must key on an IDENTITY field, never a display one.** `author_username` falls back to `nickname`, which is user-settable and not unique — filtering on it let a foreign account's video *and its ids* be returned as the requested handle's. `flatten_video` therefore also carries `author_unique_id` (raw `unique_id`, no fallback), and that is what the filter and the id provenance use. The two fields are asserted together in one test, so "harmonising" them cannot pass silently.
- **A nonexistent handle answers 404 on `/profile` but 200 with `count: 0` on `/user/posts`**, and that asymmetry is deliberate: `/user/posts` never establishes existence, and on this path "no such handle" is indistinguishable from "a real low-visibility account search did not surface". A 404 would be a confident false claim about the second case.
- **A posts page that legitimately returns nothing is `NEUTRAL`, never `EMPTY`.** Three `EMPTY`s retire the only warm identity and 503 every caller, and a run of low-visibility handles is not evidence of risk-control. Genuine risk-control still reaches identity health, because `run_call` reports every escaping `SoftError` regardless of verdict.

`client.profile()` / `user_posts()` / `_paginate_posts()` remain in the tree, tested and unreferenced, as the upgrade path: when a capture settles the real param set, only the param dict changes.

## Emptiness is per-endpoint, not global

"Did this reply carry a payload, and what does empty mean here?" has a different answer per endpoint, and a single global rule fails in both directions. `_get_signed` therefore takes a `PayloadShape` as a **required** keyword argument — no default, so a new call kind cannot silently inherit the search rule.

| Shape | Payload is | Empty means |
|---|---|---|
| `SEARCH_PAYLOAD` | any of `data` / `search_item_list` / `user_list` non-empty | risk-control, unless a live session ends in an allow-listed tail |
| `PROFILE_PAYLOAD` | a non-empty `user` dict | risk-control |
| `POSTS_PAYLOAD` | the `aweme_list` **key is present**, even when `[]` | `[]` = private / zero-post account (ordinary 200); key absent = risk-control |

Why it cannot be one rule: the search rule is `no item list AND not has_more`. A profile reply carries a `user` object, no item list and no `has_more`, so the search rule calls every **successful** profile call a shadow-block and 502s it. A posts reply for a private account carries `aweme_list: []`, which truthiness-testing would also read as risk-control. Both would retire a healthy identity for answering correctly.

`session_aware` is a single flag covering three search-session concepts together — reading `search_nil_info`, honouring `has_more`, and the continuation-tail allow-list — because a shape that read nils but ignored `has_more` would be incoherent. It also gates the tail branch, not just `has_session`: without that gate a posts reply that dropped `aweme_list` on a continuation would return as a tail 200, laundering risk-control into a silent empty.

`not_found_statuses` carries the `status_code` values that mean "no such user" and raise `NotFound` with **no retries** — re-signing cannot make a deleted user exist. It is empty for `SEARCH_PAYLOAD`, so search keeps treating every non-zero status as a retried `SoftError`. The values in `NO_SUCH_USER_STATUSES` are **not yet verified against a live reply**. A wrong value cannot launder a `hit_shark` into a 404 (risk-control answers HTTP 200 with `status_code: 0`, never a non-zero status), but it does trade that reply's identity-health report for a 404 on the profile/posts paths — so the constant is narrow and named, and one live check corrects it.

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
- Every author/user object in a search payload carries `sec_uid` — TikTok's *secure user id*. It is a stable, opaque, **public** identifier (it appears in public share URLs), grants no access and carries no session material, so it is safe to return to HTTP clients and is absent from the secret list in `.claude/rules/security.md`. It is also the identifier the user-scoped endpoints key on: `user_id` alone is not enough. Verified live: video records expose it as `author_sec_uid`, user records as `sec_uid`, both populated on real replies.
- **`search_host` serves ONLY the three search paths.** Every other `/aweme/v1/*` path on `search19-normal-alisg` is a TLB route-table miss (nginx HTML 404). Non-search endpoints must go to `api16-normal-c-*`, which accepts this client's local v46 signer and warm identity **unchanged** — proven: `/aweme/v1/user/profile/self/` returns HTTP 200 with a 107-key `user` object, and the known-good user search returns 9260 bytes of real JSON there.
- **TikTok's edge has FOUR distinct rejection shapes, and telling them apart is FREE.** An unsigned, param-less GET classifies any candidate path without spending a signature, an identity, or a daily-cap unit. This is the cheapest diagnostic on the project:

| Shape | Origin time | Means |
|---|---|---|
| TLB HTML 404, 144 B | — | path not in this host's route table |
| `{"status_code":1,"message":"Url does not match"}`, 51 B | ~7 ms | edge route-table miss |
| `{"status_code":0,"status_msg":"url doesn't match"}`, ~108 B | ~500 ms | routed, but **no handler** — a dead path |
| **bodyless 200** (`Content-Length: 0`, `Content-Type: application/json`) | ~500 ms | **live handler, request judged invalid/incomplete** |

- **A bodyless 200 is NOT `hit_shark`, and not a dead path either.** Risk-control answers HTTP 200 with a well-formed JSON body carrying `status_code: 0` and an empty item list. A zero-length *body* from a live handler means the request itself was rejected as invalid — and the same shape is what the three known-good search paths return when sent param-less, so it reads "you did not send what I need". Diagnosing it as expired credentials sends the operator to the capture loop for a problem the capture loop cannot fix.
- **There is a FIFTH rejection shape: the WAF JS challenge.** HTTP 200 with `x-tt-system-error: 3` and an HTML body carrying `_wafchallengeid` / `slardar_us_waf` / "Please wait…". It guards every HTML route on `www.tiktok.com`. A real browser solves it in ~60 s and receives a full guest session (`ttwid`, `msToken`, `tt_chain_token`, `_waftokenid`), but **that session does not transfer to `requests`** — the same cookies from curl get 403, because the WAF token is bound to the browser/TLS fingerprint, not to the cookie jar.
- **The WEB route to a user's post list is dead for a guest, and this is an identity wall, not a signing gap.** Measured with TikTok's *own* client: a real browser, WAF solved, scrolling `@khaby.lame`'s profile six times, produced `anchors=0` and a slider CAPTCHA plus a login wall. `window.byted_acrawler.frontierSign()` yields only `X-Bogus`; the web app additionally sends `X-Gnarly` and `X-Dynosaur`, for which no public encode test vector exists — so an in-process web signer would be a blind port and the project's second silently-breakable signer. Do not build one. Profile HTML carries no post list either: `__UNIVERSAL_DATA_FOR_REHYDRATION__` exposes only user/stats/`secUid` scopes.
- **`api/creator/item_list/` needs NO signature and is reachable, but rejects a param set we have not identified.** It answers `HTTP 400` with real JSON — `statusCode: 10201, "missing required fields..."` — identically for an audience-controlled account and a fully public one, and identically for the bare param set and for the complete standard web-app bundle (32 params: `WebIdLastTime`, `browser_*`, `device_id`, `from_page`, `history_len`, `screen_*`, `tz_name`, …). An informative error is a far better oracle than the mobile endpoints' bodyless 200, so this remains the cheapest web candidate — but the missing field is unknown and name-guessing it is unbounded.
- **Consequence, stated plainly: 1:1 parity with a real profile has exactly one bounded method — a mobile capture.** Every other route guesses parameter names against an endpoint that will not say which one is missing. A capture session puts the real app's query string beside ours and the diff settles it in one sitting. It needs an Android device/emulator with the v46 app, root, a system-store CA, and the documented QUIC/anti-Frida workaround.
- **`/aweme/v1/user/profile/other/` and `/aweme/v1/aweme/post/` are LIVE but reject this client's param set.** Ruled out by measurement, not inference: headers (`x-tt-store-region`, `x-tt-store-region-src`, `x-tt-target-idc`, `x-tt-request-tag`, `passport-sdk-version`), content-encoding, `sec_user_id`-only variants, region/IDC by hostname (five spellings), and target viewability (our own `sec_uid`) all leave the reply byte-identical to a bare unsigned request. The missing ingredient is a **query parameter this client does not send**; settling it needs one capture diff of the app's real query string, not more param-name roulette.
- **Dead paths — do not re-probe.** `/aweme/v1/user/detail/`, `/aweme/v1/user/info/`, `/aweme/v1/user/homepage/`, `/aweme/v1/private/aweme/post/`, all `/aweme/v2/*`, and all `/tiktok/*` answer the app-layer `"url doesn't match"` shape.
- **A user-search `user_list` node is a near-complete profile — 101 keys.** It already carries `total_favorited` (heart count), `secret` (private flag, as an **int**), `avatar_larger`/`medium`/`thumb` with `url_list`, `sec_uid`, and every follower/following/aweme count. It does **not** carry `signature` or `region` — measured null across 15 users from two independent searches, so that absence is structural, not per-user. So a profile can be served from the reply `resolve_user` already pays for, at the cost of those two fields.
- **The cursor bound is per-path, and enforced on BOTH sides.** `cursor_bound(path)` gives the three search paths `MAX_ENDPOINT_CURSOR` (100_000, an offset) and the posts path `MAX_MS_EPOCH_CURSOR` (4_102_444_800_000 — 2100-01-01Z, a *calendar* ceiling on a value that is a creation time). It is fail-closed: an unlisted or unknown path gets the strict offset bound, so no path can select a laxer one. `paging.py` spells the posts path as its own literal rather than importing it, because `client.py` imports `paging` and the reverse would close a cycle — a divergence can therefore only ever *narrow* a bound, and a test pins the two spellings equal. `encode` enforces the bound too (raising `_UNMINTABLE_CURSOR`) rather than trimming: the `seen` window is droppable state, but the cursor IS the resumable state, so there is nothing to shed. A server that mints tokens its next request rejects is worse than one that refuses to mint.
- **A producer must bound its own cursor before publishing it.** `encode`'s raise is a tripwire, not control flow. Both pagination loops clamp against `cursor_bound(path)` and retire the endpoint on a violation, so an upstream reply carrying an absurd cursor yields a 200 with the records already served and no continuation — never a `ValueError` escaping `run_in_executor` as a 500. `api/app.py` does not catch `ValueError` around `_next_page_token`, which is why the bound belongs in the producer.
- **The posts cursor runs BACKWARDS.** `/aweme/v1/aweme/post/` pages on `max_cursor`, a millisecond epoch that **decreases** page over page, where a search `offset` increases. So `_paginate_into` cannot serve it: it clamps `max(cursor, …)`, retires an endpoint whose cursor fails to strictly increase, and bounds every cursor by `MAX_ENDPOINT_CURSOR` (100_000), which a ms epoch exceeds on page one. `_paginate_posts` is a separate loop for that reason, with the guard inverted to "progress means strictly older" and the terminal ordering `not has_more` → `not advanced` (retire) → `not raw_items` (stay resumable) — so `has_more=True` is published only with a strictly-advanced cursor, and an ordinary last page is not logged as an anomaly.
- **Truncation drops the tail of the page that filled `limit`.** When a page satisfies `limit` mid-list, the cursor has already moved past that page's unemitted remainder, so those records are never served on the next `page_token` — up to 9 on search (`count=10`), up to 19 on posts (`count=20`). Deliberate and symmetric with search; the wider posts window is the cost of its larger page.
- Callers resume with `page_token` (`paging.py`), never with a bare `cursor`. `next_cursor` is informational only.
- An empty result in direct mode is an **error condition** (`SoftError`) → HTTP 502 — **but only for a sessionless first page.** A request that carried a `search_id` is a continuation, and an empty reply there is the end of the stream: it returns `200 {count: 0, has_more: false, page_token: null}` in one signed request, and does not count against identity health. Without that split, following your own `page_token` to the end of any result set retired the sole warm identity in three requests (`DEFAULT_STALE_AFTER`) and 503'd every caller.
- The continuation tail is an **allow-list** (`empty_session`, `federation_empty`, or an empty page with no nil block at all) that **fails closed**: `hit_shark` or any nil this code does not recognise still raises `SoftError` → 502 and still reports the empty, so a session can never launder risk-control. An allow-listed tail is terminal for that endpoint regardless of the `has_more` the reply carries — trusting `has_more: true` on a reply already classified as terminal produced an unbounded chain of empty pages at one paid signature each.

## ADRs

| # | Title | Status |
|---|-------|--------|
| _(none yet)_ | | |
