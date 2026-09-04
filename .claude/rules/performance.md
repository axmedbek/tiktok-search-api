---
paths:
  - "mobile/tiktoksearch/**"
---
# Performance discipline

- **Reuse the `requests.Session` per client** (already done in `client.py` / `rapid_signer.py`). Never create a `Session` per request.
- **Respect the per-device daily cap** and don't hammer. Retries rotate `_rticket`/`ts`; back off via `time.sleep` inside `_get_signed`.
- **Stop paginating promptly** on `has_more=false` or a non-advancing cursor. Don't over-fetch.
- **RapidAPI costs money + quota.** Sign exactly once per request; never sign speculatively. Switch provider on quota exhaustion, not on empty results.
- **`run_merged` fan-out is bounded by pool size and dedupes.** Don't fan out beyond available slots.
- **Keep the hot path allocation-light.** Don't parse the full raw response when only `data[]` / `search_item_list[]` is needed.
- **gzip is handled by `requests` automatically.** Don't double-decompress.
