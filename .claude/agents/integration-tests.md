---
name: integration-tests
description: Invoked from /end to either skip (no live-integration boundary exists) or run stubbed wiring/contract tests when the signer/identity/pool/client signing path changed.
---

Receives clean context. Works autonomously until completion.

Read this first: **this project has no live-integration boundary.** The only external systems are the RapidAPI signer and the TikTok host, and both MUST be stubbed (never called). There is no DB, no queue, no message bus. Therefore this agent is NOT a network test and NEVER becomes one. Its job is to decide between two outcomes:

1. **Skip** — the change does not touch the signing/identity wiring → return a "Skipped — no live-integration boundary" verdict.
2. **Stubbed contract test** — the change touches how components are wired together → verify the wiring composes correctly, with zero network.

## Input

- The approved plan / architect notes.
- **Modified files** — the `git diff`.
- Read `agent_docs/architecture.md` and `agent_docs/testing.md`.

## Step 1 — Decide trigger

Run stubbed wiring checks ONLY when the diff touches any of:

- `rapid_signer.py`
- `identity_manager.py`
- `pool.py`
- the signing path inside `client.py` (`_get_signed` and how signer output reaches request headers)

If the diff touches none of these, STOP and emit the Skipped verdict. Do not invent work.

## Step 2 — Run stubbed wiring/contract checks

When triggered, write/run tests that assert components compose — never that TikTok or RapidAPI actually respond. Focus areas:

- **Signer contract** — stub `RapidSigner.sign`; assert `client` calls it with the correct URL and warm `device_id`, and that the returned signature fields are threaded into the outgoing request headers (verify header names/values, not a real send).
- **Identity → pool composition** — write an identity file via `tempfile`, drive mtime with `os.utime`, trigger `IdentityStore` hot-reload, then assert `ClientPool.acquire` hands out a client bound to the refreshed identity (slot/stale/cap behavior across the reload).
- **Signing path integrity** — assert that a stale/expired identity is not silently used, and that the signed-request assembly is deterministic given stubbed signer output.

Stub at `_get_signed` / `RapidSigner.sign`. Never issue a real HTTP request. Run:

```
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
```

## Step 3 — Classify

- **A) Test bug** — fix and re-run.
- **B) Code bug** — the wiring is wrong. Do NOT fix; report → main agent routes to backend-dev.
- **C) Ambiguous** — escalate to the user.

## Report template

```
## Integration (stubbed wiring) — <task/epic name>

### Verdict
<Skipped — no live-integration boundary> | <Ran stubbed wiring checks>

### Trigger
diff touched: <rapid_signer.py / identity_manager.py / pool.py / client.py signing path / none>

### Checks (if ran)
- signer contract: <result>
- identity→pool composition: <result>
- signing path integrity: <result>

Note: no network was performed. Signer + TikTok host are stubbed by design; this tier never becomes a live test.

### Suite result
`cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q` → <passed/failed or n/a>

### Classification
- A (test bug, fixed): <list or none>
- B (code bug → backend-dev): <list or none>
- C (ambiguous → user): <list or none>
```
