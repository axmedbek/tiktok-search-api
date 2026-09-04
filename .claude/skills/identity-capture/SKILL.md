---
name: identity-capture
description: Mint or refresh fresh warm TikTok identities (sessionid cookie + x_tt_token + device fingerprint) using the rooted-emulator + mitmproxy capture loop that hot-reloads identities.json — use for capture, warm identity, refresh cookie, x_tt_token, sessionid, emulator, frida, mitmproxy, identities.json, expired.
---

# Identity Capture (warm cookie + x_tt_token)

Operational / manual work performed **outside** the app. This is how expired identities (root cause #1 of hit_shark) get refreshed.

## Why you can't just HTTP a login
A mobile `sessionid` is minted by the native app through device attestation + captcha + the native MSSDK login flow. There is no plain-HTTP path to obtain it. You must drive the real logged-in v46 app and capture the credentials off the wire.

## The sanctioned capture loop
1. **Rooted Android emulator** — API 36 / arm64, Magisk, mitmproxy CA installed into the **system + APEX** trust store, `frida-server` running. Run the logged-in **v46** TikTok app on it.
2. **mitmdump** with the capture addon intercepts the app's traffic:
   ```bash
   mitmdump -s /Users/axmedbek/PhpstormProjects/tiktok-searcher/mobile/capture_identity_addon.py
   ```
3. The addon watches for credential changes and **atomically writes** `/Users/axmedbek/PhpstormProjects/tiktok-searcher/mobile/identities.json` when cookie / x_tt_token change.
4. The app **hot-reloads** on file mtime via `mobile/tiktoksearch/identity_manager.py` (`IdentityStore`) — no restart needed.

## `identities.json` schema (per identity)
```
device_id     matching the v46-activated app
iid           install id
cookie        includes sessionid=...   (SECRET)
x_tt_token    (SECRET)
user_agent    v46 UA string
device_query  { cdid, openudid, region:"US", mcc_mnc, carrier_region, timezone, host_abi, ... }
```
All of `device_query` must match the same v46-activated app that produced the cookie, or the identity reads as cold.

## Add / rotate an identity manually
Edit `mobile/identities.json` (git-ignored — never commit or print it), add a full identity object with every field above, save. `IdentityStore` picks it up on mtime change and re-runs health. Verify with `GET /health`.

## HARD BLOCKER — plain system proxy does NOT capture TikTok
ByteDance TTNet / Cronet **ignores the system `http_proxy`** and speaks **QUIC over UDP 443**, so a standard mitmproxy system-proxy setup sees nothing. To capture you need one of:
- a **transparent proxy / VPN** (redirect UDP 443, or force TCP by blocking QUIC), or
- a **Frida Cronet hook** to divert the app's network stack.

Anti-frida in the app blocks heavy Java-bridge agents, so keep the Frida agent minimal (native/Cronet hook, not a full java bridge).

## Reference
`agent_docs/common-changes-identity.md`.
