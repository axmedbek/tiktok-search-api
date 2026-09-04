---
name: pagination-harvesting
description: Harvest many results across pages by chaining cursor+search_id over the two merged search endpoints — use for pagination, cursor, next_cursor, has_more, search_id, offset, load more, merge, dedup, more results, or limit questions.
---

# Pagination & Harvesting

Getting many results (not just the first ~30) is the whole point. The logic is in `client.py` `_paginate_into` and, for direct mode, the two-endpoint merge above it.

## The two endpoints (client.py)
- `SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'` → items in `data[]`
- `SEARCH_ITEM_PATH  = '/aweme/v1/search/item/'`         → items in `search_item_list[]`

Why merge both: `single/` alone stops at ~30 with `has_more=false`. Chaining the Videos-tab `search/item/` endpoint and deduping into the same set yields more results — matching what the phone actually shows. Direct mode loops over both `((SEARCH_VIDEO_PATH,'data'),(SEARCH_ITEM_PATH,'search_item_list'))`, both calling `_paginate_into` with a **shared** `out`/`seen` so cross-endpoint duplicates are dropped.

## _paginate_into mechanism
- `cursor = start_cursor`; request param `cursor=str(offset)` is the previous response's cursor.
- Next offset: `data.get('cursor', cursor + len(raw_items))`.
- `has_more = bool(data.get('has_more'))`.
- **search_id chaining** (direct mode): echo the prior response's `log_pb.impr_id` (fallback `extra.logid`) back as `params['search_id']` on the next request — this is session continuity, without it TikTok resets the window.
- **count=10** in direct mode — deeper pagination; `count=20` makes TikTok return `has_more=false` early.
- **Dedup**: each record's key (aweme `id` for videos, `username` for users) is checked against the shared `seen` set; duplicates skipped.
- **Stop conditions**: `not has_more or not raw_items`; OR non-advancing guard — if `added == 0 and cursor == start_cursor` (cursor didn't move and nothing new), break.

## Mixed data[] gotcha
`data[]` is MIXED: real videos (`type=1`, carry `aweme_info`/`aweme_id`) plus user cards and ads (no `aweme_id`). The unwrap returns the item only if it has an `aweme_id`; `flatten_video` (`mapping.py`) skips anything without one. NEVER assume `data[0]` is a video.

## Client cursor / next_cursor
Client-facing cursor is a simple offset into the merged result list, not TikTok's raw cursor. `SearchPage.next_cursor = start_cursor + len(out)` when `has_more`, else `None`. Callers pass `next_cursor` back as `cursor` to continue.

## fan_out (pool.run_merged)
`run_merged(query, fan_out)` clamps `fan_out` to `[1, pool size]`, queries that many devices in parallel via a `ThreadPoolExecutor`, and merges+dedupes all their pages into one `SearchPage` (each device returns a shallow window from a different identity/proxy, so union = more unique results). Costs one daily-cap unit per device used.

## Caller pagination via the API
Fetch page 1, then feed `next_cursor` back as `cursor` until `has_more` is false / `next_cursor` is null:

```bash
# page 1
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":50}'
# → {... "next_cursor": 48, "has_more": true, ...}

# page 2 — pass the returned next_cursor as cursor
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"type":"keyword","query":"ocean","limit":50,"cursor":48}'
```

Cross-ref: memory `common-changes-api.md`, memory `real-search-pagination-findings.md`, memory `direct-api-WORKS.md`, `.claude/skills/fastapi-service/SKILL.md`.
