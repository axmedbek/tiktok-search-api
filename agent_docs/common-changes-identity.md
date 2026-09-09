# Common Changes — Identity / Anti-block

## Add or rotate a warm identity
1. Fresh identities come from the capture loop (emulator + `capture_identity_addon.py` → `identities.json`). To add manually, append an object to `mobile/identities.json`:
   ```json
   {"device_id":"...","iid":"...","cookie":"...; sessionid=...","x_tt_token":"...","user_agent":"...","device_query":{...}}
   ```
2. The running API hot-reloads on file mtime change — no restart. Confirm via `GET /health` → `identities` block (`usable` count rises).
3. Never commit `identities.json` (git-ignored — holds live secrets).

## Add a proxy
- `config_direct.yaml` → `proxies:` list, one full URL per line: `scheme://user:pass@host:port` (`http` or `socks5`; socks5 needs `requests[socks]`, already in requirements).
- Assigned round-robin to identities/devices in `pool.py` `_build_slots`. Use **residential/mobile** proxies — datacenter IPs are blocked.

## Tune health thresholds
- `IdentityStore(path, stale_after=N)` — consecutive empty results before an identity is marked stale (`identity_manager.py` `DEFAULT_STALE_AFTER = 3`).
- Wire a config knob through `api/app.py` if it should be configurable per deployment.

## Change the capture flow
- `capture_identity_addon.py` runs under mitmproxy alongside the emulator, NOT inside the app. It only rewrites `identities.json` when creds actually change (avoids mtime churn).
- The hard blocker for capture is that ByteDance TTNet/Cronet ignores the system HTTP proxy (QUIC/UDP 443) — needs transparent proxy/VPN or a Frida Cronet hook. See project memory `emulator-traffic-setup-state`.
- **The addon sees every request the app makes but records only identity material.** That matters, because a capture session is now the project's one bounded route to the real user-scoped endpoints: `/aweme/v1/aweme/post/` and `/aweme/v1/user/profile/other/` are live but reject this client's param set, and they give no feedback about what is missing (a bodyless 200). Guessing param names is unbounded; putting the app's real query string beside ours settles it in one sitting. See `architecture.md` § data flow invariants for what was already ruled out — headers, encoding, host/region, and the whole web route.

## Diagnose "empty / 100-100" results
1. `GET /health` — is any identity `usable`, or are they all `stale`? All stale → creds expired, refresh via capture loop.
2. Check logs for `empty result (attempt N, device=..., path=...)` — that's `hit_shark`, an identity problem, not a signer problem.
2b. **Read the reply BODY before concluding identity.** `hit_shark` is HTTP 200 with well-formed JSON, `status_code: 0`, and an empty item list. A *bodyless* 200, an nginx/TLB 404 page, a `"url doesn't match"` JSON, or a WAF challenge page are all something else entirely — wrong host, dead path, incomplete params, or a browser gate — and diagnosing any of them as expired credentials sends you to the capture loop for a problem it cannot fix. `architecture.md` § data flow invariants has all five shapes and how to tell them apart for free.
3. Confirm the identity has a `sessionid` cookie + `x_tt_token`; guest requests get empty results for many keywords.
4. Confirm a residential proxy is assigned — bare host IP is its own block vector.
