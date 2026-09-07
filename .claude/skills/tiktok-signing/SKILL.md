---
name: tiktok-signing
description: How TikTok v46 mobile API requests are signed (argus/gorgon/ladon/khronos/x-ss-stub), the `signer: local | rapid | legacy` modes, why the vendored pure-Python signer DOES work on v46 when fed the sign_* params plus a warm identity, and how signing is wired into the client — use for sign, argus, gorgon, ladon, khronos, x-ss-stub, signer, local signer, 403, RapidAPI, mssdk, provider quota.
---

# TikTok v46 Request Signing

Every call to the TikTok mobile API must carry four ByteDance signature headers or it is rejected (403). This project produces them **in-process with the vendored signer** by default (`signer: local`, zero quota); the remote RapidAPI signer is the fallback and the stale-key diagnostic.

## The four signature headers
- `x-argus` — main integrity/attestation token (native MSSDK; version-locked to app_version).
- `x-gorgon` — request-hash token (over path + params + optional body stub).
- `x-ladon` — companion token derived from khronos + license.
- `x-khronos` — request timestamp (unix seconds); everything else is bound to it.
- `x-ss-stub` — only when there IS a request body: `x-ss-stub = MD5(plaintext_body).hexdigest().upper()`. The search endpoint used here is a query-string GET with no body, so x-ss-stub is usually absent.

## The vendored signer DOES work on v46 — `signer: local` is the default
`mobile/tiktoksearch/signing.py` + `tiktok_signer/**` was long believed dead on v46 ("keys frozen at v37"). That conclusion confounded two variables: `signing.py` fed `MetasecSigner` the **v32** `ClientConfig` defaults (`app_version 32.9.4`, `sdk_version_code 41090`, `license_id 11512`) while only `RapidSigner` read the `sign_*` v46 values, and `MetasecSigner.sign()` never attached the warm identity (`cookie` / `x_tt_token`).

Fed the `sign_*` v46 params **plus** the warm identity, it returns real results. Only `DEFAULT_SIGN_KEY` and `GORGON_TABLE` are static in `metasec.py`; `app_version` / `sdk_version` / `sdk_version_code` / `license_id` are **arguments**. It holds because `mssdk_ver_code 83952160` is shared between the 37.x and 46.x builds — the MSSDK that produces `x-argus` did not change.

Measured: 15/15 consecutive searches, 150 records, keyword/hashtag/user, both filters, `page_token` chaining with zero overlap — all at **zero** RapidAPI quota, at parity with the paid signer including its shared failures.

`signer:` selects the mode (`config.py` `resolved_signer()`):

| mode | signer | path |
|---|---|---|
| `local` | vendored, via `MetasecSigner.for_v46()` (maps `sign_*` → the params `Metasec.sign` reads) | direct: `search_host`, `count=10`, `search_id`, hit_shark detection |
| `rapid` | `RapidSigner` | direct |
| `legacy` | vendored on the v32 defaults, **no** identity | the old cold path (`api_hosts`, `count=20`) — empty by design |
| unset | `rapid` if `rapidapi_key` else `legacy` | preserves pre-knob behaviour |

❌ Still never "fix" empty results by editing the vendored signer — an empty is `hit_shark`, i.e. identity, per the **anti-block-evasion** skill.

## The one risk: a static sign key, and how to tell when it goes stale
`DEFAULT_SIGN_KEY` / `GORGON_TABLE` come from a specific `libmetasec_ov.so` build. When TikTok bumps `mssdk_ver_code`, local signing breaks — and it breaks **silently**: no exception, a well-formed signature that risk-control rejects with HTTP 200 + empty `data[]`, i.e. indistinguishable from identity staleness. `/health` will show a usable identity while every search 502s as `hit_shark` and `IdentityStore` retires credentials that were never the problem.

**Discriminator:** *every* identity going stale at once, shortly after a TikTok app release, points at the sign key. Flip one profile to `signer: rapid` and re-run the same query — one paid sign, and the only signal that separates a stale key from stale credentials. This is why RapidAPI stays configured: as a diagnostic instrument and a hard-failure fallback, not as an answer to empties.

## RapidSigner — the fallback path (`mobile/tiktoksearch/rapid_signer.py`)
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

## The fallback is narrow on purpose
`client.py` `_sign()` → `_rapid_fallback()` triggers **only** on a signer-level `TransportError` (the signing call itself failed) and only in `local` mode with a key configured. It can never fire on `SoftError`/hit_shark/an empty result — `SoftError` and `TransportError` are siblings, and `SoftError` is raised downstream of `_sign`. Widening it would re-create the misdiagnosis `.claude/rules/lessons/anti-block.md` forbids: provider switching is for quota, empties are for identity refresh.

## Adding a new signer provider
Add a provider branch in `mobile/tiktoksearch/rapid_signer.py` (request shape + response header mapping), expose it via `config.py`, and select it in the YAML profile. Follow `agent_docs/common-changes-api.md`.

## CRITICAL: a valid 200 + real x-argus does NOT guarantee results
A signer that returns valid headers and an endpoint that returns HTTP 200 can still give empty `data[]`. That is **hit_shark** (identity distrust), not a signing bug. Do not touch the signer for empties — go to the **anti-block-evasion** skill.
