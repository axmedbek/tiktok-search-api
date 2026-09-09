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

## Add a new upstream (TikTok) endpoint
0. **Establish which HOST serves it, before anything else.** `search_host` serves only the search paths and answers everything else with a real nginx/TLB 404. `_get_signed` picks `search_host` for both direct modes and `api_hosts` only for `legacy`, so a new non-search endpoint has no host to go to until that switch is made per endpoint. Probe the path on both host families first: a 404 means wrong host, a zero-length 200 means the request is not reaching the application layer, and a JSON body with a `status_code` means you have the right host and can start on params.
1. Add its path constant in `client.py` and put it in the path allow-list `paging.decode` is given — a `page_token` names a path that ends up in a SIGNED URL, so it is whitelisted, never merely type-checked.
2. **Define a `PayloadShape` for it.** Reusing `SEARCH_PAYLOAD` is the trap: the search rule 502s any reply that carries no item list and no `has_more`, which includes every successful non-search reply. Decide explicitly what its payload key is, whether present-but-empty is legitimate, and whether a non-zero `status_code` means "no such thing" (→ `NotFound`, no retries) or a soft reject (→ retried `SoftError`).
3. Pass the shape to `_get_signed`; it is a required keyword argument, so this cannot be forgotten.
4. Flatten its payload in `mapping.py` (see "Map a new response field").
5. Map any new domain error in `api/app.py`. The error map is **inline per handler**, not central — a domain exception left unmapped escapes `run_in_executor` as a 500.

## Change pagination behavior
- Logic lives in `client.py` `_paginate_into`. Preserve the invariants: dedupe by `id`/`username`, stop on `has_more=false` or non-advancing cursor, echo `search_id` from `log_pb.impr_id` in direct mode.
- Test with stubbed `_get_signed` returning canned pages — never the network.

## Map a new response field
- Extend `flatten_video` / `flatten_user` / `flatten_profile` in `mapping.py`. Return `None` for items without `aweme_id` (skip user cards/ads).
- Coerce through the module's existing helpers, never inline: `to_int` for a count, `_str_or_none` for an id, `_verified` for the verified rule, and **`_flag` for any numeric 0/1 flag** — a bare `bool()` on a flag inverts silently when the value arrives as the string `'0'`.
- Adding a key is additive and safe: `SearchResponse.results` is `list[dict[str, Any]]`, and `paging.py` fingerprints a record's id string rather than its dict, so a new key cannot perturb response validation or cross-request dedup. Renaming or removing one is not — that breaks live callers.
- Add the field to `SearchResponse`'s record shape expectation if clients depend on it.

## Verification (any API change)
```bash
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000 &
curl -s -X POST http://127.0.0.1:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"ocean","type":"keyword","limit":5}' | python3 -m json.tool
```
