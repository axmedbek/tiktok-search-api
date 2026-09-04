---
name: tiktok-signing
description: How TikTok v46 mobile API requests are signed (argus/gorgon/ladon/khronos/x-ss-stub) via the RapidAPI signer, why the vendored pure-Python signer fails, and how signing is wired into the client — use for sign, argus, gorgon, ladon, khronos, x-ss-stub, signer, 403, RapidAPI, mssdk, provider quota.
---

# TikTok v46 Request Signing

Every call to the TikTok mobile API must carry four ByteDance signature headers or it is rejected (403). This project produces them with a remote RapidAPI signer, not the vendored code.

## The four signature headers
- `x-argus` — main integrity/attestation token (native MSSDK; version-locked to app_version).
- `x-gorgon` — request-hash token (over path + params + optional body stub).
- `x-ladon` — companion token derived from khronos + license.
- `x-khronos` — request timestamp (unix seconds); everything else is bound to it.
- `x-ss-stub` — only when there IS a request body: `x-ss-stub = MD5(plaintext_body).hexdigest().upper()`. The search endpoint used here is a query-string GET with no body, so x-ss-stub is usually absent.

## Why the vendored signer fails on v46 (do not use to fix empties)
`mobile/tiktoksearch/signing.py` + `mobile/tiktoksearch/tiktok_signer/**` is a pure-Python signer whose keys are frozen at **v37**. It produces v37-era `x-argus` that the v46 endpoint rejects → 403 or HTTP 200 with empty `data[]`. All open-source signers share these frozen keys. `config_signed.yaml` uses this signer and returns empty **by design** (cold, for testing the pipeline only). Never "fix" empty results by editing the vendored signer.

## RapidSigner — the real path (`mobile/tiktoksearch/rapid_signer.py`)
Host: `tiktok-api-signer.p.rapidapi.com`. Two providers, switchable for quota:

- **tiktanic** (primary, `config_direct.yaml`): `POST /android/get_sign` with `dev_info` + `payload`.
- **working** (`config_working.yaml`): `POST /sign` with `url` + `device_model` + `headers`.

Both return `x-argus` / `x-gorgon` / `x-ladon` / `x-khronos`, which are merged into the outgoing request headers.

### v46 params (must match the v46-activated app exactly)
```
app_version           46.0.42
mssdk_ver_str         v05.01.02-alpha.7-ov-android
mssdk_ver_code        83952160
license_id            2142840551
app_id / aid          1233
```
A version mismatch here silently yields empty results even with a 200 from the signer.

### Concrete call to the tiktanic provider
```bash
curl -s -X POST 'https://tiktok-api-signer.p.rapidapi.com/android/get_sign' \
  -H 'x-rapidapi-host: tiktok-api-signer.p.rapidapi.com' \
  -H "x-rapidapi-key: $RAPIDAPI_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "dev_info": {"aid":1233,"app_version":"46.0.42",
                 "mssdk_ver_code":83952160,
                 "license_id":2142840551},
    "payload": "https://search19-normal-alisg.tiktokv.com/aweme/v1/general/search/single/?keyword=ocean&count=10&offset=0&search_source=normal_search"
  }'
# -> {"x-argus":"...","x-gorgon":"...","x-ladon":"...","x-khronos":"..."}
```

## How signing is wired into the client (`mobile/tiktoksearch/client.py`)
`_get_signed` builds the full signed URL first (query params assembled), calls the signer to obtain the four headers, merges them onto the request headers (plus x-ss-stub if a body exists), then performs the GET. Search endpoint:
`GET https://search19-normal-alisg.tiktokv.com/aweme/v1/general/search/single/` with `keyword` / `count` / `offset` / `search_source` in the query string (Videos-tab variant: `/aweme/v1/search/item/`).

## Adding a new signer provider
Add a provider branch in `mobile/tiktoksearch/rapid_signer.py` (request shape + response header mapping), expose it via `config.py`, and select it in the YAML profile. Follow `agent_docs/common-changes-api.md`.

## CRITICAL: a valid 200 + real x-argus does NOT guarantee results
A signer that returns valid headers and an endpoint that returns HTTP 200 can still give empty `data[]`. That is **hit_shark** (identity distrust), not a signing bug. Do not touch the signer for empties — go to the **anti-block-evasion** skill.
