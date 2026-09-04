---
name: anti-block-evasion
description: Diagnose and defeat ByteDance risk-control (hit_shark) where HTTP 200 returns empty data[] because the identity is distrusted, not because signing is broken — use for empty results, hit_shark, 100/100 empty, arada boş, shadow-block, risk-control, search_nil_info, blocked, no results.
---

# Anti-Block Evasion (hit_shark)

## Doctrine
An empty response is an **identity problem, not a signer problem**. The endpoint returns HTTP 200 with empty `data[]` (a.k.a. `search_nil_info` / hit_shark) when ByteDance distrusts the caller's identity. A valid signer with real `x-argus` cannot fix this. Never swap or edit the signer to chase empties.

## Ordered diagnostic (do these in order)
1. **Health first:** `GET /health` — are there usable (non-stale, healthy) identities in the IdentityStore? If zero usable, the fix is identity refresh, stop here (see identity-capture).
2. **Logs:** look for `empty result (attempt N, device=..., path=...)` in the app logs — this tells you which device_id and endpoint path hit the wall.
3. **Creds present?** For that identity, confirm `sessionid` cookie **and** `x_tt_token` are both present and non-expired in `mobile/identities.json`. (Never print the values.)
4. **Proxy assigned?** Confirm a residential proxy is assigned/round-robined for that identity's egress IP.
5. **Only then** consider signer / app_version / endpoint. This is last, not first.

## Four root causes, ranked
1. **Expired warm credentials (#1, most common)** — sessionid / x_tt_token aged out. Fix: refresh via capture loop, not the signer.
2. **Cold device** — device_id/iid never properly v46-activated or missing full `device_query` fingerprint (cdid, openudid, region=US, mcc_mnc, carrier_region, timezone, host_abi).
3. **Version mismatch** — signer/app params drift from v46 (app_version 46.0.42, mssdk 83952160, license 2142840551). See tiktok-signing skill.
4. **Bad IP** — datacenter / flagged / geo-mismatched egress. Fix: residential proxy matching identity region.

## Invariants (keep these true)
- Empty must surface as a `SoftError` → HTTP **502**, never a silent 200 with empty list. If empties leak as 200, that is a bug in the error path (`mobile/tiktoksearch/errors.py`, `client.py`).
- `IdentityStore` (`mobile/tiktoksearch/identity_manager.py`) must track health + staleness and exclude dead identities from the pool.
- App version must match **v46** across signer params and device fingerprint.
- Egress uses residential-proxy round-robin, one stable IP per identity where possible.

## What NOT to do
- Do NOT swap or "upgrade" the signer to fix stale-credential empties.
- Do NOT retry the same request without rotating `_rticket` / `ts` (khronos) — an identical replay just re-triggers the same block.
- Do NOT commit or print `mobile/identities.json` contents.

## Cross-references
- Refresh creds: **identity-capture** skill.
- Egress IPs: proxy-management (residential round-robin).
- Deep dive: `agent_docs/architecture.md` § hit_shark and `agent_docs/common-changes-identity.md`.
