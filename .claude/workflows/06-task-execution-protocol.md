---
name: task-execution-protocol
description: Execute an approved plan through the quality pipeline — delegated implementation → parallel review → parallel testing → docs + lessons. Launched by /dev after the user approves a plan from /start. Unit tests run separately in /end.
duration: varies by complexity
prerequisites: Approved plan from /start (Task or Epic level)
---

# Task Execution Protocol

Executes an approved plan through the quality pipeline with specialized agents.

**Entry point:** user runs `/dev` after approving a plan. Classification and planning are done.

**Core principle:** the main agent orchestrates and NEVER writes production code. The only exception is a `[GENERAL]` architect finding scoped to config/docs. All production Python goes through `backend-dev` via `Task()`. There is no `frontend-dev` — this project has no UI.

**Where unit tests live:** unit tests are written in `/end` (after developer approval), in parallel with documentation work. This workflow covers wiring/contract checks (`integration-tests`) and API verification (`api-verifier`) — they verify behavior for the developer's approval.

---

## Red flags — if you're rationalizing, STOP

| Thought | Reality |
|---------|---------|
| "Run spec-compliance, then architect" | No. Launch BOTH in parallel in ONE tool-use block. Sequential is a workflow violation. |
| "Run integration, then api-verifier" | No. Parallel in one block when both apply. |
| "architect / spec review isn't needed, it's a tiny change" | Both stages are mandatory. No skipping. |
| "Empty results — I'll just swap the signer" | Almost always wrong. Empty = `hit_shark` = identity (expired creds / cold device / bad IP). Diagnose identity first (`.claude/rules/anti-block.md`). |
| "Scope grew a bit, I'll add it silently" | Stop. Report to the user. |
| "Skip the doc checkpoint, I'll write it all in /end" | No. By `/end` the context is gone. The checkpoint is 3 lines. Mandatory for Epics. |
| "Skip Phase 4 — update docs in /end" | No. Phase 4 = Doc Refresh + Learned Lessons while pipeline context is fresh. `no updates / no lessons` is a valid answer. |
| "I'll write unit tests here" | No. Unit tests are written in `/end`. Here only wiring checks + api-verifier. |
| "Re-escalate a 4th time" | Limit is 3 iterations. After that, document the remainder in the completion message. |
| "The main agent can write this one small function" | No. All production code goes through `backend-dev`. |

---

## Pipeline

Every task — Task or Epic subtask — follows the same pipeline:

**Phase 1 (Decompose + Delegate) → Phase 2 (Parallel Review: spec + architect) → Phase 3 (Parallel Testing: integration-wiring + api-verifier) → Phase 4 (Documentation Refresh + Learned Lessons)**

- Phase 2 issues → fix via `backend-dev` → **re-run BOTH spec + architect in parallel** (max 3 iterations)
- Phase 3 failures → fix via `backend-dev` → re-run the failed agent only (max 3 iterations)

---

### Phase 1 — Decompose + Delegate

The main agent reads the approved plan. This project is backend-only, so scope is one dimension. Delegate to `backend-dev` via `Task()`.

**Invocation:**
```
Task() → backend-dev

Scope: [specific list of what to implement — endpoints, config fields, pool/identity/client/signer/mapping logic]
Plan: [path to approved plan or implementation_plan.md]
Files to modify: [full paths — agent reads contents itself]
```

**Doc-file deliverables (CRITICAL).** If the plan calls for updates to `agent_docs/*.md` or `.claude/rules/*.md` as part of THIS task's deliverable, list those paths in `Files to modify` with one-sentence direction each — otherwise the subagent treats docs as someone else's job and spec-compliance flags the drift in Phase 2, burning a fix cycle.

**After the subagent completes:** review its report. Deviations or blockers → stop and report to the user.

---

### Phase 2 — Parallel Review (spec compliance + architect)

⚠️ **Launch BOTH subagents in a SINGLE response with parallel `Task()` calls.** Sequential is a workflow violation.

```
Task() → spec-compliance-reviewer
Approved plan: [paste]
Modified files: [full paths]
Test files: [paths if any]

Task() → architect-review
Iteration: [1, 2, or 3]
Task plan: [paste]
Modified files: [full paths]
Read: agent_docs/architecture.md, agent_docs/agents-config.md, .claude/rules/ (esp. anti-block.md, api-service.md, security.md for the changed paths)
```

**Combine results:**

| Spec | Architect | Action |
|------|-----------|--------|
| COMPLIANT | APPROVED | Proceed to Phase 3 |
| COMPLIANT | CHANGES REQUIRED | Fix architect issues only |
| GAPS FOUND | APPROVED | Fix spec gaps only |
| GAPS FOUND | CHANGES REQUIRED | Fix BOTH in one delegation pass |

**Routing architect fixes by tag:**

| Tag | Route to |
|-----|----------|
| `[BACKEND]` | `backend-dev` via Task() (architect remarks + file paths) |
| `[TESTS]` | skipped here — unit tests run in `/end` |
| `[GENERAL]` | main agent fixes directly (config/docs only) |

*(There is no `[FRONTEND]` tag in this project.)*

**Re-escalation:** after fixes → re-run BOTH spec + architect in parallel at iteration N+1. **Limit 3.** At iteration 4, document remaining issues in the completion message and proceed.

---

### Phase 3 — Parallel Testing (integration-wiring + api-verifier)

⚠️ **When both apply, launch in a SINGLE response with parallel `Task()` calls.**

**Conditions:**

| Agent | Run when |
|-------|----------|
| `integration-tests` | Diff touches the signer/identity/pool/client signing wiring (`rapid_signer.py`, `identity_manager.py`, `pool.py`, `client.py` signing path). It runs STUBBED wiring/contract checks only — never the network. If none of these touched → it returns Skipped. |
| `api-verifier` | Change affects API request/response behavior — endpoints, schemas, mapping, pagination, error mapping. Verifies via curl against a locally-run server. |

If only one condition is met → invoke only that agent. If neither → skip Phase 3, go to Phase 4.

**Step 3.0 — Environment pre-flight (main agent, BEFORE the api-verifier Task()).** api-verifier needs a running server. Probe and, if needed, start it here so the subagent assumes a healthy environment:
```bash
curl -sIm 3 http://127.0.0.1:8000/health | head -1   # any status line = up
# if not up, start it (background), then re-probe:
# .venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000
```
If the server can't be started or all identities are stale (health shows 0 usable) → note it; api-verifier will (correctly) classify empties as an identity/environment problem, not a code bug.

**Invoke:**
```
Task() → integration-tests
Approved plan: [path]
Architect notes: [CRITICAL/MAJOR items]
Modified files: [list]

Task() → api-verifier
Task plan: [paste]
Changed API surface: [endpoints/schemas/mapping affected]
api-test-cases.md: [path, if it exists — use as the checklist]
```

**Combine + route fixes:**
- `integration-tests` code bug (Category B) → `backend-dev` via Task().
- `api-verifier` failure classified **A (code/contract bug)** → `backend-dev` via Task().
- `api-verifier` failure classified **B (identity/environment)** → escalate to the user (stale creds / no proxy). Do NOT blame or "fix" the code.
- Category C (ambiguous) → escalate with a hypothesis.

**Re-escalation:** re-run only the failed agent. **Limit 3.**

---

### Phase 4 — Documentation Refresh + Learned Lessons

**Authority:** all writes to `agent_docs/*` and `.claude/rules/lessons/*` / `learned-lessons.md` follow `.claude/rules/agent-docs-standards.md`. Read it before the first write of this phase (it is path-scoped, so not in context until you open one of those files).

**Step 1 — scope of changes:** `git diff --name-only` (staged/HEAD).

**Step 2 — update agent_docs incrementally:**

| File | Update if… |
|------|-----------|
| `architecture.md` | New module/responsibility, changed data flow, new integration point, new signer/identity mechanism, new ADR |
| `testing.md` | New test helper/fake/pattern/convention |
| `common-changes-api.md` / `common-changes-identity.md` (+ index row) | This task is a reusable change pattern |
| `agents-config.md` | Commands, versions, config profiles, or deps changed |

REPLACE stale sections; no full rewrites; no append blocks. Only touch files that exist.

**Step 3 — ADR (if an architectural decision was made):** new pattern, significant dependency, config/schema/structure change, a rejected alternative worth recording. If yes → create `docs/adr/NNN-short-name.md` and add a row to the ADR table in `architecture.md`. If no → skip.

**Step 4 — Learned Lessons.** Check each trigger; skip any that didn't happen:

| # | Trigger |
|---|---------|
| 1 | Developer corrected Claude this session |
| 2 | A re-escalation cycle hit iteration 3 |
| 3 | Architect review found gaps |
| 4 | Spec compliance found gaps |
| 5 | api-verifier / integration wiring failed from an implementation error |

If none fired → output `Phase 4: no lessons`. Otherwise write per `agent-docs-standards.md` (invariant-phrased `Fix:`, reconcile by identifiers, route domain lessons to `.claude/rules/lessons/<domain>.md`, process lessons to `learned-lessons.md`). Max 5 new rules per task.

**Step 5 — if nothing needed updating → `Phase 4: no doc updates / no ADR / no lessons`.** No silent skip.

---

## Completion message

```
✅ [Task/Epic] complete: [brief]

Changed files:
- path — what changed

Spec compliance: ✅ COMPLIANT (iteration N)
Architect review: ✅ APPROVED (iteration N)
Integration (wiring): ✅ passed / ⏭️ no signer/identity/pool wiring change
API verification: ✅ [checks] / ⚠️ identity/env issue (escalated) / ⏭️ no API-behavior change
Docs refreshed: [files] / — none
Learned lessons: +N / — no lessons
```

Then remind the user to run `/end` to write unit tests, run pytest, commit, and push.

---

## Epic-specific additions

### Doc checkpoint (after each subtask pipeline completes)

Append under the subtask's `[x]` line in `implementation_plan.md`:
```markdown
- [x] Subtask N: [name]
  **Changes:** [created/modified files]
  **Decisions:** [key design decisions, 1-2 sentences]
  **Gotchas:** [unexpected issues, if any]
  **Test focus:** [2-4 concrete bullets — what unit tests for THIS subtask must cover]
  **Architect risks:** [verbatim CRITICAL/MAJOR from this subtask's architect-review, or "none"]
```
`Test focus` and `Architect risks` are how the `/end` unit-tests subagent recovers context when the Epic finishes in a later session.

---

## Edge cases

- **Unexpected complexity discovered mid-implementation** → stop, report, do not silently expand scope.
- **Security issue found** (leaked secret, identities.json exposure) → stop immediately, report before touching code.
- **Iteration limit (3) reached in Phase 2 or 3** → document the remainder in the completion message; don't block delivery indefinitely.
- **All identities stale during Phase 3** → this is an environment/credential problem, not a code failure. Report to the user (refresh via the capture loop); do not attempt code fixes.
