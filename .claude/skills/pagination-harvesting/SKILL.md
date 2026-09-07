---
name: pagination-harvesting
description: Harvest many results across pages by chaining page_token (cursor+search_id session) over the two merged search endpoints — use for pagination, page_token, cursor, next_cursor, has_more, search_id, offset, load more, merge, dedup, more results, or limit questions.
---

# Pagination & Harvesting

Getting many results (not just the first ~30) is the whole point. The logic is in `client.py` `_paginate_into` and, for direct mode, the two-endpoint merge above it.

## The two endpoints (client.py)
- `SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'` → items in `data[]`
- `SEARCH_ITEM_PATH  = '/aweme/v1/search/item/'`         → items in `search_item_list[]`

Why merge both: `single/` alone stops at ~30 with `has_more=false`. Chaining the Videos-tab `search/item/` endpoint and deduping into the same set yields more results — matching what the phone actually shows. Direct mode loops over both `((SEARCH_VIDEO_PATH,'data'),(SEARCH_ITEM_PATH,'search_item_list'))`, both calling `_paginate_into` with a **shared** `out`/`seen` so cross-endpoint duplicates are dropped.

## _paginate_into mechanism
- `cursor = start_cursor`; request param `offset=str(cursor)` is the previous response's cursor.
- Next offset: **TikTok's own** `data['cursor']` (fallback `cursor + len(raw_items)`), clamped monotone with `max(cursor, …)` so a tail reply carrying `cursor: 0` cannot rewind a stream. TikTok's value is authoritative: dedup drops items, so a page can return 15 records while TikTok's cursor is 20.
- `has_more = bool(data.get('has_more'))`.
- **search_id chaining** (direct mode): echo the prior response's `log_pb.impr_id` (fallback `extra.logid`) back as `params['search_id']`. This is the SESSION. Without it a request at `offset > 0` does not "reset the window" — TikTok answers `search_nil_item: empty_session` and returns nothing at all. Measured identically on the local and the RapidAPI signer, so it is session state, never a signing or identity problem.
- `_paginate_into` takes a `search_id` seed so a session survives ACROSS HTTP requests, and publishes progress into a `PageProgress` holder even when it later raises — a transport failure mid-stream must not amputate an endpoint that already served records.
- **count=10** in direct mode — deeper pagination; `count=20` makes TikTok return `has_more=false` early.
- **Dedup**: each record's key (aweme `id` for videos, `username` for users) is checked against the shared `seen` set; duplicates skipped.
- **Stop conditions**: `not has_more or not raw_items`; a non-advancing guard that compares against the **previous iteration's** cursor (`cursor <= prev_cursor`) rather than the initial one — the old `cursor == start_cursor` form could only ever fire on iteration 1, so a stuck upstream cursor looped forever at one paid sign per turn; and `MAX_PAGES_PER_ENDPOINT` as a backstop. Both stops write `has_more=False`, retiring the endpoint for later requests too.

## Mixed data[] gotcha
`data[]` is MIXED: real videos (`type=1`, carry `aweme_info`/`aweme_id`) plus user cards and ads (no `aweme_id`). The unwrap returns the item only if it has an `aweme_id`; `flatten_video` (`mapping.py`) skips anything without one. NEVER assume `data[0]` is a video.

## page_token — how callers actually paginate
`next_cursor` is now **TikTok's own cursor**, and it is *informational only*. A caller cannot resume from a bare offset: `cursor: 10` with no session returns `502 empty_session`. Continuation is `page_token`, an HMAC-authenticated `base64url(json).base64url(tag)` blob (`paging.py`) carrying, per endpoint, `(path, cursor, search_id, has_more, started)`, plus an `hmac`-derived device handle and a bounded window of recent record-id fingerprints.

- **The device is pinned** by a stable `hmac(identity_key)` handle, never the positional `id{i}` label — an `identities.json` reorder would otherwise repoint `id0` at a different device and send a foreign `search_id` on a healthy identity.
- **`seen` is seeded from the token** (`MAX_SEEN_FINGERPRINTS = 240`, counted in **records** — four pages only at the shipped `max_results_per_search: 60`), so an endpoint opened mid-stream cannot re-emit records an earlier page already served. Beyond that horizon a repeat is undetectable.
- **Tokens die on restart** (the HMAC secret is per-process) → `422`, not a resumable session. The service is single-process by design; `uvicorn --workers` was never supported.
- **`fan_out`**: a session lives on one device, so an explicitly supplied `fan_out > 1` with a `page_token` is a `422`, while the server's `default_fan_out` is silently coerced to 1. A merged (`run_merged`) page mints no token at all.
- Rejected token (malformed, tampered, wrong query, out of bounds) → **422**, never a 502.

## fan_out (pool.run_merged)
`run_merged(query, fan_out)` clamps `fan_out` to `[1, pool size]`, queries that many devices in parallel via a `ThreadPoolExecutor`, and merges+dedupes all their pages into one `SearchPage` (each device returns a shallow window from a different identity/proxy, so union = more unique results). Costs one daily-cap unit per device used.

## Caller pagination via the API
Fetch page 1, then feed `page_token` back until it comes back `null`. ❌ Never feed `next_cursor` back as `cursor` — that is a sessionless request and answers `502 empty_session`.

```bash
# page 1
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":10}'
# → {... "count": 10, "next_cursor": 10, "has_more": true, "page_token": "eyJ2Ijox…" }

# page 2 — pass the token back, nothing else changes
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":10,"page_token":"eyJ2Ijox…"}'
```

Verified live: three chained pages returned 30 distinct ids with **zero** overlap, served by the same pinned device. The end of a stream is `200` with `count: 0`, `has_more: false`, `page_token: null` — **not** a 502.

Cross-ref: memory `common-changes-api.md`, memory `real-search-pagination-findings.md`, memory `direct-api-WORKS.md`, `.claude/skills/fastapi-service/SKILL.md`.
