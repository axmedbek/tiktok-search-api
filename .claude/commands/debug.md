# Debug a search / empty-results bug

Triage the #1 real bug class: **empty results / "100/100 empty" / "arada boş qaytarır"** (intermittent empty 200s). Follow the ordered diagnostic below. The most common root cause is **expired warm credentials (`hit_shark`), not a signing bug** — do not swap the signer to "fix" empties caused by stale creds.

## Ordered diagnostic

**(1) Health — are any identities usable?**
```
curl -s localhost:8000/health
```
- **All stale / 0 usable** → credentials expired. This is **NOT a code bug**. Refresh via the capture/identity-refresh loop and stop here. See `agent_docs/common-changes-identity.md`.
- Some usable → continue.

**(2) Logs — is this `hit_shark`?**
Grep server logs for the empty-result marker:
```
empty result (attempt N, device=..., path=...)
```
Its presence = risk-control rejection (`hit_shark`), surfaced as `SoftError` → HTTP 502. See `architecture.md` § hit_shark.

**(3) Identity completeness — cookie + token present?**
Confirm the identity in use has a real `sessionid` cookie **and** `x_tt_token`. A **guest** identity (missing either) returns empty by design. (Inspect health/pool state — never print `mobile/identities.json`.)

**(4) Proxy — is a residential proxy assigned?**
No/bad proxy (datacenter IP, wrong geo) triggers `hit_shark` even with warm creds. Confirm the identity has a residential proxy bound.

**(5) Signer — only after identity is ruled out.**
Inspect the signing path last: `rapid_signer.py`, the version match (`sign_app_version` must be **v46**), and endpoint + query params (`single/` + `search/item/`, `offset`/`search_id`/`count`). A v37-era argus against v46 risk-control returns empty — but this is a version/config issue, not a reason to churn signers.

## Output

Produce, in order:
1. A **hypothesis** naming the layer (identity → proxy → signer/version → mapping).
2. A **minimal repro** (the exact `curl` + which identity/device, secrets masked).
3. A proposed fix **only after** identity + proxy are ruled out; if the cause is expired creds or missing proxy, the "fix" is a credential refresh / proxy assignment — escalate to the user, not a code change.

## Rules

- Do **not** "fix" empties by swapping the signer when the cause is expired creds or a missing residential proxy.
- Never read or print `mobile/identities.json` or any cookie / `x_tt_token`.
- Escalate identity/environment failures to the user; only genuine code defects go to `backend-dev`.
- Cross-refs: `agent_docs/common-changes-identity.md`, `agent_docs/architecture.md` § hit_shark, `.claude/commands/verify.md`, `.claude/rules/anti-block.md`.
