---
name: reverse-engineering
description: Toolchain and hard-won facts for capturing and decoding TikTok mobile-search traffic in this project; use when you reverse engineer, capture traffic, use mitmproxy/frida/disasm, decode protobuf/gzip, or fight TTNet/Cronet/libmetasec/argus internals.
---
# reverse-engineering

Operational/research backdrop for the signer and identity-capture work here. This is a v46 anti-bot target — treat everything below as replay-verified facts, not guesses.

## Capture rig
- Rooted Android emulator, **API 36 / arm64**, Magisk.
- mitmproxy CA installed into **both** the system store and the APEX `cacerts` (Android 14+ splits them).
- `frida-server` (matching abi) running on device.
- **CLEAN-RELAUNCH recipe** (load hooks before the first network call):
  1. `am force-stop com.zhiliaoapp.musically`
  2. relaunch the app
  3. **immediately** attach the unpin/hook agent — hooks must be live before the first net call or you miss the warm requests.

## The CRITICAL blocker: TTNet/Cronet ignores the system proxy
ByteDance's TTNet is built on Cronet and does **not** honor the Android system `http_proxy` for real API traffic — it uses **QUIC / UDP 443**. Only the `tnc0` bootstrap respects the proxy, so mitmproxy sees bootstrap but not the search calls. Two ways through:
- **Transparent proxy / VPN redirect** (force UDP443 through the interceptor), OR
- **Frida hook into Cronet request-building** to dump URL + headers + body directly, including the real v46 `x-argus`.

## Anti-frida
The app scans `/proc/self/maps` for `frida`/`gum`, checks `TracerPid`, and probes port `27042`. It tolerates a brief attach but blocks heavy java-bridge agents. Use **frida 16.6.6** (has the `Java` global); **17.x split `Java` out** into a separate bridge — don't upgrade blindly.

## Response format
Real search responses are **HTTP-chunked, gzip'd, multi-object JSON streams**. To decode:
1. de-chunk, 2. gunzip, 3. split the concatenated top-level JSON objects, 4. take the object that has `data[]` / `search_item_list[]`.
Request bodies are **protobuf**; `x-ss-stub = MD5(plaintext body)`.

## Signer paths
- **Vendored pure-Python signer — the default** (`signer: local`). It DOES sign v46: `MetasecSigner.for_v46()` feeds it the `sign_*` params and attaches the warm identity, and `DEFAULT_SIGN_KEY` still holds because `mssdk_ver_code 83952160` is shared between the 37.x and 46.x builds. The old "403s on v46, keys frozen" verdict measured a real failure but named the wrong cause. Re-extracting the key from `libmetasec_ov.so` becomes necessary only when TikTok bumps the MSSDK.
- **Paid RapidAPI v46 signer** (`rapid_signer.py`) — now the fallback for a hard signing failure, and the diagnostic for a stale local key.
- **Free: Frida-hook the app's own native MSSDK signer** as a local RPC. Discover the class by enumerating `ms.bd*` / `com.ss.*` for the method signature `(int, int, long, String, Object)` — that's the argus entry point (libmetasec native).

See also the tiktok-signing and identity-capture skills — this is their shared backdrop.
