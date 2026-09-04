---
paths:
  - "mobile/tiktoksearch/**"
  - "mobile/*.py"
---

# Anti-Block / Risk-Control Discipline

The central problem of this project. Read before touching `client.py`, `pool.py`, `identity_manager.py`, or the signer.

## Core doctrine
An empty result (`HTTP 200` with empty `data[]`) is **hit_shark risk-control**, i.e. an **IDENTITY problem, not a signer bug**.
- Diagnose identity FIRST: is the identity healthy / non-stale? Are cookie **and** token present? Is a proxy assigned?
- ❌ Do NOT "fix empties" by changing the signer when the real cause is expired warm creds / a cold device / a bad IP. A valid signature still returns empty under hit_shark.

## Invariants that MUST hold
- **(a) Empties surface as `SoftError`.** A shadow-blocked/empty direct-mode result is raised as `SoftError` (→ HTTP 502), so callers can distinguish "genuinely no results" from "shadow-blocked". ❌ Never return a silent `200` with empty records for a shadow-block.
- **(b) Health gating.** Identities carry health; N consecutive empties → stale. The pool (`pool.py`) must not hand out a stale identity. A fresh cookie/token resets health.
- **(c) Warm identity requirements.** A warm identity needs: `sessionid` cookie + `x_tt_token` + full `device_query` fingerprint + a signer `app_version` that matches (v46). A version mismatch triggers hit_shark — keep signer and device version aligned.
- **(d) Proxy hygiene.** Use residential/mobile proxies, round-robin. ❌ Datacenter IPs and one-IP-serving-many-devices are block vectors.
- **(e) Credential minting is external only.** The capture loop (mitmproxy + `capture_identity_addon.py`, outside the app) is the ONLY sanctioned way to mint fresh creds. It rewrites `identities.json` atomically and only on a real credential change; the app hot-reloads on mtime (`identity_manager.py`). ❌ Do not hand-edit `identities.json` or mint creds inside the app.

## Retries & caps
- A retry MUST rotate timestamps (`_rticket` / `ts`) — ❌ never re-send the identical signed request.
- Respect the per-device daily cap and the UTC day roll in `pool.py`.

## When you see empties
Check, in order: identity health/staleness → cookie+token present → proxy assigned/type → signer `app_version` vs device version. Only after all pass is the signer itself a suspect.
