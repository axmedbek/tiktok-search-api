---
name: proxy-management
description: Add, format, rotate, and health-check proxies for the TikTok search pool — use when dealing with proxy, residential, socks5, IP block, rotation, 429, rate limit, or datacenter-IP issues.
---

# Proxy Management

Proxies are the single most important anti-block lever. Datacenter IPs are blocked by TikTok; one IP serving many device_ids gets the whole IP 429'd. Use RESIDENTIAL/mobile proxies only.

## Config format
Proxies live in the `proxies:` list in `mobile/config_*.yaml` (e.g. `mobile/config_direct.yaml`). Each entry is a URL string:

```yaml
# mobile/config_direct.yaml
proxies:
  - http://user123:pass456@proxy.example.com:8080     # HTTP proxy
  - socks5://user123:pass456@proxy.example.com:1080    # SOCKS5 proxy
```

Format: `scheme://user:pass@host:port`. Scheme is `http` or `socks5`. SOCKS5 needs the `socks` extra (`requests[socks]`), already in `mobile/requirements.txt`. Auth is optional (`scheme://host:port` works for open proxies). Parsed in `config.py` `PoolConfig.from_mapping` into the frozen tuple `proxies` (empty/None entries are filtered out).

## How the pool assigns them
`pool.py` `_build_slots` builds a `next_proxy()` closure that hands out `config.proxies` round-robin per device slot: identity#1 -> proxies[0], identity#2 -> proxies[1], wrapping with `proxies[proxy_idx % len(proxies)]`. Every device/identity gets `overrides['proxy'] = next_proxy()`. So N proxies spread across M devices — aim for >= 1 unique residential proxy per device so no IP hosts multiple device_ids.

Startup logs `%d proxied / %d direct` (`ClientPool.__init__`). Status/health masks credentials via `_mask_proxy` (line 19, pool.py) — never logs or returns the raw `user:pass`.

## Verify a proxy works
Curl through it before adding to config:

```bash
curl -x http://user123:pass456@proxy.example.com:8080 -s https://api.ipify.org
# socks5:
curl -x socks5h://user123:pass456@proxy.example.com:1080 -s https://api.ipify.org
```

Expect the proxy's egress IP (should be residential/mobile, not a datacenter ASN). Then check the running API:

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
```

`/health` reports the proxied device count and per-device masked proxies. `proxied` should equal (or nearly) the device count.

## Anti-block interaction
An unproxied bare-host egress IP is its own `hit_shark`/shadow-block vector: all your device_ids share the server's one IP and get correlated → 429/empty-200. Never run production without residential proxies. On 429/RateLimited (mapped to HTTP 429 in `api/app.py`) or empty-200 symptoms, add/rotate proxies first.

Cross-ref: `.claude/skills/anti-block-evasion/SKILL.md`, memory `common-changes-identity.md`, memory `intermittent-empty-200-diagnosis.md`.
