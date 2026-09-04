# Testing

## Framework & layout

- **pytest.** Tests in `mobile/tiktoksearch/tests/`, one file per module under test (`test_<module>.py`).
- Run: `cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q`
- Style: class-based grouping (`class TestSearchFilters:`), plain `assert`, `pytest.raises(ValueError)` for validation failures. No fixtures framework beyond stdlib `tempfile` where a file is needed.

## What to test (pure logic, no network)

The value of this suite is the pure, deterministic logic. Cover:

- **config.py** — `from_mapping` drops unknown keys; `with_overrides` merges non-empty values and tuple-izes `api_hosts`; frozen dataclass immutability.
- **filters.py** — `SearchFilters.to_query_params` emits flat v46 params + legacy blob; `SearchQuery` validation (empty term, negative cursor, filters-on-user rejected); `keyword`/`source_term` derivation.
- **identity_manager.py** — health transitions (N consecutive empty → stale; `report_ok` clears), hot-reload on mtime change, health preserved on same-cred rewrite but reset on new cookie/token, `overrides()` shape, `usable_count`. Use `tempfile` + `os.utime` to drive mtime.
- **pool.py** — slot built per identity, `acquire` skips stale slots, all-stale → `PoolExhausted` with the identity-specific message, daily cap + UTC day roll, `run_merged` dedup. Inject a fake client so no real search happens.
- **mapping.py** — `flatten_video` skips items without `aweme_id`, maps stats/author/music correctly, `to_int` coercion; `flatten_user`.
- **client.py pagination/dedup** — `_paginate_into` dedupes by key, stops on `has_more=false` / non-advancing cursor, merges two endpoints. Stub `_get_signed` to return canned pages — never sign or hit the network.

## What NOT to unit-test

- RapidAPI signer HTTP calls, live TikTok search, the capture addon, the emulator loop. These need live identities/proxies/emulator and are verified manually (see `.claude/agents/api-verifier.md` for the curl-based API check).
- Never let a unit test make a real request to TikTok or RapidAPI. Stub at the `_get_signed` / `RapidSigner.sign` boundary.

## Fakes

- Fake signer: an object with `.sign(url=..., device_id=..., iid=...) -> {}` returning a dict of dummy headers.
- Fake client for pool tests: object exposing `.device_id`, `.iid`, `.proxy`, `.search(query) -> SearchPage`.
- Canned TikTok response: a dict with `data`/`search_item_list`, `has_more`, `log_pb.impr_id`, `search_nil_info` to exercise both success and hit_shark branches.
