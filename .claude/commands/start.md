# Session Start

Plan-only intake. Research a request, classify it as **Task** or **Epic**, produce a concrete plan, and stop at a confirmation gate. This command executes **no** implementation work — `/dev` does that.

## ABSOLUTE RULE

`/start` writes **NO project files**. Two exceptions only:
- `implementation_plan.md` — written **verbatim** at the end of Phase E, and only for an **approved Epic**.
- `api-test-cases.md` — written by the `test-case-writer` subagent in Phase D.5, and only when the change affects API request/response behavior.

Everything else (production code, tests, docs, CHANGELOG) is out of scope here. Never edit `mobile/`, `agent_docs/`, `.claude/`, or config files during `/start`.

---

## Step 0 — Date & language (silent)

- Note today's date.
- Read `## Communication Language` from `CLAUDE.local.md`. Respond to the user in that language. If absent, default to English and note it.

## Step 1 — Git state (silent)

- `git status` and current branch.
- Read `## Git Workflow` from `agent_docs/agents-config.md`. Record: **base = main**, **rebase target = none** (single-branch; rebase is skipped at `/end`), Conventional Commits = no, CI = none.

## Step 2 — Active Epic detection (silent)

```
find . -maxdepth 4 -name implementation_plan.md -not -path '*/.venv/*'
```

If one exists, an Epic is already in flight. Tell the user and recommend `/dev` to continue it rather than starting new work — unless their request is clearly unrelated.

## Step 3 — Docs presence (silent)

Confirm `agent_docs/` exists (`agents-config.md`, `architecture.md`, `testing.md`, `common-changes.md`). If missing, tell the user to run `/onboard` first.

---

## Step 4 — Task Intake

### Phase A — Research (silent)

Identify the **domain** of the request, then read the routed docs. Do not narrate every read; surface only what changes the plan.

| Signal in request | Domain | Route |
|---|---|---|
| endpoint, `/search`, param, response field, pagination, cursor, new signer provider, config knob | **api** | `common-changes.md` → `common-changes-api.md` |
| cookie/token expiry, warm identity, proxy, health thresholds, capture loop, `hit_shark`, empty results | **identity-anti-block** | `common-changes.md` → `common-changes-identity.md` |
| argus/gorgon/x-ss-stub, `rapid_signer.py`, `tiktok_signer/`, version match | **signer** | `common-changes-api.md` + `architecture.md` § signer |
| ClientPool, rotation, cap, busy/503 | **pool** | `common-changes-api.md` + `architecture.md` |
| YAML profiles, `config_*.yaml`, `config.py` | **config** | `agents-config.md` § Config files |
| `mapping.py`, flatten, `SearchPage` shape | **mapping** | `common-changes-api.md` |

Always:
- Read `agent_docs/architecture.md`.
- Read `agent_docs/testing.md` if tests are in scope.
- Read `common-changes.md` (the index), then the routed `common-changes-*.md`.
- `grep`/`find` for related code; read **2–4** of the most relevant files (e.g. `client.py`, `api/app.py`, `api/schemas.py`, `identity_manager.py`, `rapid_signer.py`).
- Use **Context7 MCP** for external-library specifics (FastAPI / requests / pydantic) when the change depends on library behavior — do not guess API surface from memory.

### Phase B — Classify Spike vs Task vs Epic (always shown)

First ask: **is the outcome code, or knowledge?** If you cannot say what code changes because the request depends on discovering how TikTok/the signer/risk-control currently behaves — it is a **Spike**, not a plannable Task/Epic. Do not force a research question into a code plan.

| | Spike | Task | Epic |
|---|---|---|---|
| Outcome | verified knowledge + recommendation | a known code change | a known multi-part change |
| You know what to build? | **no — must find out first** | yes | yes |
| Files touched | none (throwaway probes only) | ≤ 5 | > 5 |
| Modules | n/a | single module | multi-module |
| When in doubt | — | — | **choose Epic** |

**Spike signals:** "why is it empty / returning empty", "is the signer stale / is TikTok on a new version", "did the endpoint / response shape change", "can we capture X", "is provider Y also blocked", any root-cause hunt where the fix is unknown.

If the request is a **Spike** → stop this flow and tell the user: **"This is a research question — run `/spike <the question>`."** Do not produce a Task/Epic plan for it. (`/spike` will hand back a concrete change to `/start` if one turns out to be warranted.)

Otherwise state the Task/Epic classification explicitly and why. Epics get an `implementation_plan.md` and subtask tracking; Tasks do not.

### Phase C — Clarifying questions

Ask only what blocks planning. Caps: **bug ≤ 2**, **feature ≤ 3**, **refactor ≤ 1**. Every question carries a **recommendation** (your default if the user says "you pick"). If nothing is blocking, skip and say so.

### Phase D — Plan

There is **no Frontend Tasks** section (no UI). "User-visible" = the HTTP API, verified with `curl`.

**Task template:**
```
## Plan: <title>   [TASK]

### Context
<1–3 lines: what & why, domain>

### Change
- <file>: <edit>
- <file>: <edit>

### Contracts
<request/response fields, error mapping, signer/version constraints — only if API-facing>

### Test Scope
<pytest targets and/or curl scenarios that must pass>

### Verification
- Unit: `cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q`
- API (if behavior-facing): run server, then curl:
  `.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000`
  `curl -s localhost:8000/health` ; `curl -s -XPOST localhost:8000/search -H 'content-type: application/json' -d '{"query":"ocean","type":"video","limit":10}'`

### Out of scope
<explicit non-goals>
```

**Epic template:**
```
## Plan: <title>   [EPIC]

### Context
<what, why, domain, risk (hit_shark? identity? signer version?)>

### Contracts
<endpoints, request/response schema deltas, error → HTTP mapping, signer app-version (v46) constraints>

### Backend Tasks
<grouped by module: client.py / api / identity / signer / config / mapping>

### Subtasks
- [ ] <smallest shippable unit>
- [ ] <next>
- [ ] ...

### Test Scope
<per-area: unit (pytest) + integration-tests (if signer/identity/pool/client-signing) + api-verifier (if API behavior)>

### Verification
<the actual pytest + curl commands, as in the Task template>

### Rollback / safety
<how to revert; never touch mobile/identities.json>
```

### Phase D.5 — API Test Cases

If the change **affects API request/response behavior** (new/changed endpoint, param, response field, error status, pagination), launch the writer:

```
Task(subagent_type="test-case-writer", mode="Generate", plan=<the plan above>)
```

It writes `api-test-cases.md` at repo root (curl scenarios, expected status, expected response shape). **Skip** for pure-internal, config-only, signer-internal, or docs-only changes — say why you skipped.

### Phase E — Confirmation Gate

Present the plan and ask for approval. Interactive loop:
- **Change requested** → revise the plan; if `api-test-cases.md` exists and is affected, re-run `test-case-writer` in **Revise** mode. Re-present.
- **Approved** →
  - **Task**: confirm the user should now run `/dev`. Write **no** file.
  - **Epic**: write the plan **verbatim** to `implementation_plan.md`, then tell the user to run `/dev`.

Do not begin implementation. `/start` ends at approval.

---

## Rules

- Never write production code, tests, docs, or CHANGELOG in `/start`.
- Only files creatable here: `implementation_plan.md` (approved Epic) and `api-test-cases.md` (via subagent).
- Never read or print `mobile/identities.json`.
- Always show the Task/Epic classification.
- When in doubt on scope → Epic.
- Cross-refs: `agent_docs/architecture.md`, `agent_docs/common-changes*.md`, `.claude/workflows/06-task-execution-protocol.md`, `.claude/rules/*`.
