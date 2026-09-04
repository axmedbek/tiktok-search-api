# Common Changes — API / Search

## Add a new query param to search
1. Add it to the `build()` closure in `client.py` `_search_videos` / `_search_users`.
2. If client-facing, add it to `SearchRequest` (`api/schemas.py`) and thread through `_to_query` (`api/app.py`).
3. If it's a filter, extend `SearchFilters` (`filters.py`) and its `to_query_params` — emit the **flat v46 param** (v46 ignores the legacy `filter_selected` blob).

## Add a new signer provider
1. Add a `_sign_<provider>` method to `RapidSigner` (`rapid_signer.py`) returning `{x-argus, x-ladon, x-gorgon, x-khronos}`.
2. Branch on `cfg.rapidapi_provider` in `RapidSigner.sign`.
3. Document the provider + its config fields in `config.py` and add a `config_<provider>.yaml`.
4. Validate the provider's 200-but-empty failure mode — raise `TransportError` when headers are missing (don't return empty headers).

## Add a new config knob
1. Add the field to `ClientConfig` or `PoolConfig` (`config.py`) with a default. Keep the dataclass frozen.
2. `from_mapping` auto-picks it up (it filters by field names) for `PoolConfig`; for nested `ClientConfig` it's read via `from_mapping` too.
3. Document it inline in `config.py` and in the relevant `config_*.yaml` with a comment.

## Change pagination behavior
- Logic lives in `client.py` `_paginate_into`. Preserve the invariants: dedupe by `id`/`username`, stop on `has_more=false` or non-advancing cursor, echo `search_id` from `log_pb.impr_id` in direct mode.
- Test with stubbed `_get_signed` returning canned pages — never the network.

## Map a new response field
- Extend `flatten_video` / `flatten_user` in `mapping.py`. Use `to_int` for numeric coercion. Return `None` for items without `aweme_id` (skip user cards/ads).
- Add the field to `SearchResponse`'s record shape expectation if clients depend on it.

## Verification (any API change)
```bash
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000 &
curl -s -X POST http://127.0.0.1:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"ocean","type":"keyword","limit":5}' | python3 -m json.tool
```
