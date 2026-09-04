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

## Diagnose "empty / 100-100" results
1. `GET /health` — is any identity `usable`, or are they all `stale`? All stale → creds expired, refresh via capture loop.
2. Check logs for `empty result (attempt N, device=..., path=...)` — that's `hit_shark`, an identity problem, not a signer problem.
3. Confirm the identity has a `sessionid` cookie + `x_tt_token`; guest requests get empty results for many keywords.
4. Confirm a residential proxy is assigned — bare host IP is its own block vector.
