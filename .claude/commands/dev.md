# Execute Approved Plan

Execute a plan approved in `/start` through the mandatory quality pipeline. The main agent **orchestrates only** — it never writes production code; all implementation is delegated to `backend-dev`.

## Decision logic

```
implementation_plan.md exists on disk?
 ├─ yes → MODE 2: continue the Epic (resume next unchecked subtask)
 └─ no  → a plan was approved in THIS session?
          ├─ yes → MODE 1: execute the approved Task/Epic plan
          └─ no  → ERROR: "No approved plan. Run /start first."
```

## Mandatory Quality Gates

Every subtask runs the full pipeline in `.claude/workflows/06-task-execution-protocol.md`. Do not skip a phase.

```
Decompose + Delegate ──► Parallel Review ──► Parallel Testing ──► Docs Refresh + Learned Lessons
   (backend-dev)         (2 agents, 1 block)   (0–2 agents, 1 block)
```

1. **Decompose + Delegate** — hand the concrete change to `backend-dev`. There is **no frontend-dev** (no UI). The main agent must not edit `mobile/**` source itself.
2. **Parallel Review** — launch **both** in a **single tool-use block**:
   - `spec-compliance-reviewer` (matches plan/contracts)
   - `architect-review` (structure, module boundaries, `code-standards.md`)
   Address blocking findings via `backend-dev`, then continue.
3. **Parallel Testing** — launch the applicable agents in **one** tool-use block:
   - `integration-tests` — **only if** the change touched the **signer / identity / pool / client-signing** path.
   - `api-verifier` — **only if** the change affects **API request/response behavior** (`POST /search`, `GET /health`, params, response fields, error status, pagination).
   - If both apply, launch both together in the same block. If neither applies (pure config/docs/internal helper), state that and skip. Unit tests do **not** run here — they run in `/end`.
4. **Docs Refresh + Learned Lessons** — update the affected `agent_docs/*` sections in place (per `agent-docs-standards.md`); record any new standing invariant in `.claude/rules/lessons/<domain>.md` or `learned-lessons.md`.

## Branch management

- Base = **main** (from `agent_docs/agents-config.md` § Git Workflow).
- If on `main`, create a working branch from `origin/main`:
  ```
  git fetch origin && git switch -c feat/<short-name> origin/main
  ```
  Use `fix/`, `refactor/`, `test/`, `docs/`, `chore/` prefixes as appropriate.
- Do not commit or push here — `/end` handles that.

---

## Mode 1 — Execute new plan (this session)

1. Ensure a working branch (above).
2. Run the pipeline once (Task) or per subtask (Epic-in-session). For an Epic approved this session, `implementation_plan.md` was already written by `/start`; treat it as Mode 2 from here.
3. On completion, tell the user to run `/end`.

## Mode 2 — Continue an Epic

1. Read `implementation_plan.md`.
2. Find the **first** `[ ]` subtask.
3. Run the full Quality Gates pipeline for that subtask.
4. On green, mark it `[x]` in `implementation_plan.md` and add a one-line checkpoint note (what changed, which tests ran).
5. Repeat for the next `[ ]`, or stop when the user asks. If a subtask expands API behavior, run `test-case-writer` in **Append** mode to extend `api-test-cases.md`.
6. When all subtasks are `[x]`, report the Epic is complete (the plan file is deleted in `/end`).

---

## Completion

Report: what was implemented, review outcomes, which test agents ran and their results, docs/lessons touched, current branch. Then: **run `/end`** to finalize (hygiene, unit tests, CHANGELOG, commit, push).

## Rules

- Main agent **never** writes production code — delegate to `backend-dev`.
- Review agents always run in parallel in one block; test agents likewise when >1 applies.
- `integration-tests` gates on signer/identity/pool/client-signing; `api-verifier` gates on API behavior.
- Never read or commit `mobile/identities.json`.
- Follow `.claude/workflows/06-task-execution-protocol.md` exactly.
