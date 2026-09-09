---
paths:
  - "mobile/tiktoksearch/paging.py"
---

# Lessons — Pagination tokens

Domain lessons for the opaque `page_token`. Loaded only when `paging.py` is in play. Format per `.claude/rules/agent-docs-standards.md`.

- **The server mints a `page_token` that its own next request rejects with 422.** A bound was enforced on `decode` only. `encode` checked nothing about the cursor, so a producer publishing an out-of-range value got a well-formed token back and the failure surfaced one request later, on the caller, as a malformed-token 422 — pointing at the client for a server bug. **Fix:** the cursor bound and the size cap are enforced on `encode` as well as `decode`. `encode` raises `_UNMINTABLE_CURSOR` rather than trimming, because the `seen` window is droppable state while the cursor IS the resumable state — there is nothing to shed. The remaining field bounds (`MAX_ENDPOINTS`, `MAX_ENDPOINT_PATH_CHARS`, the path allow-list, `MAX_SEARCH_ID_CHARS`, `TOKEN_VERSION`, the device-handle length) stay decode-only and are held at ingest by their producers.

- **A bound added to the validator turns an upstream oddity into an HTTP 500.** `encode`'s new raise is a tripwire, not control flow: nothing catches `ValueError` around `_next_page_token` in `api/app.py`, so an upstream reply carrying an absurd cursor took down a request that had already served records. **Fix:** every pagination loop clamps against `cursor_bound(path)` and retires the endpoint on a violation, so the caller gets a 200 with the records already served and no continuation. A bound lives in the producer; the validator's copy is defence in depth.

- **A size-cap assertion of the form `len(encode(worst_case)) <= MAX_PAGE_TOKEN_CHARS` passes under any cap, including one that is too small.** `encode` sheds `seen` fingerprints oldest-first to fit, so shrinking the cap silently destroys cross-request dedup state instead of failing. **Fix:** a derivation test asserts the **untrimmed** `_wire` length fits AND that every fingerprint survives the round trip; and it builds its worst case from the field bounds directly, never by calling `_worst_case()` — an assertion that restates the definition it is checking cannot detect a wrong definition.
