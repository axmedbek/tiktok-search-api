# Session End

Finalize the session: hygiene, unit tests, docs audit, CHANGELOG, commit, push. Rebase target is **none**, so rebase and CI-watch are skipped.

## Step 1 — Analyze session

Summarize what changed this session (files, modules, plan/subtasks completed). Identify the domain(s) touched to route the later steps.

## Step 2 — Hygiene

Delete throwaway artifacts created during the session:
- `debug-*`, `temp-*`, `scratch-*`, `*.tmp`, `*.bak`
- the working `api-test-cases.md` (a QA scratch file, not committed)

Never delete source under `mobile/`, tests, `agent_docs/`, `.claude/`, or configs. **Never touch `mobile/identities.json`.**

## Step 2b — Unit tests (parallel, non-blocking)

Launch the unit-tests subagent and continue to the docs audit while it runs:
```
Task(subagent_type="unit-tests", scope=<modules changed this session>)
```

## Step 3–5 — Docs audit

Read **every** file under `agent_docs/` (`agents-config.md`, `architecture.md`, `testing.md`, `common-changes.md`, `common-changes-api.md`, `common-changes-identity.md`). Update **stale sections in place** per `.claude/rules/agent-docs-standards.md` (REPLACE, don't append; no date stamps; no status headers). Only touch sections the session actually made stale — commands, contracts, module table, config profiles, test scope.

## Step 6 — CHANGELOG

Always update `CHANGELOG.md` (create it if absent). One concise entry describing the user-facing/behavioral change. **Never end a session without a CHANGELOG entry.**

## Step 7 — Reconcile unit tests

Wait for the unit-tests subagent, then:
- **A) all pass** → continue.
- **B) new gaps** → have `unit-tests` add the missing tests.
- **C) code bug found** → route the fix to `backend-dev`, re-run.
- **D) test is wrong** → fix the test.

**Epic cleanup:** if `implementation_plan.md` has every subtask `[x]`, delete it now.

## Step 8 — Commit

- `git add -A` (respecting `.gitignore`; `mobile/identities.json` is ignored — **never** commit or force-add it).
- Short **imperative** subject (no Conventional Commits). End the message with the required co-author trailer.
- **Never** revert or discard working-tree files. Never modify infra files (rules/agents/commands/workflows) except learned-lessons.

## Step 9 — Pre-push gate (pytest)

```
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
```
Must be green before pushing. If red, fix (route code bugs to `backend-dev`) and re-run.

## Step 10 — Rebase — SKIP

Rebase target = none. Do not rebase.

## Step 11 — SKIP

No rebase → nothing to resolve/re-run.

## Step 12 — Push

```
git push --force-with-lease
```
(Set upstream on first push: `git push -u origin <branch>`.)

## Step 12a — CI-watch — SKIP

No CI configured. Report **"CI: ⏭️ not configured"**.

## Step 13 — Final report

Branch, commit subject, files changed, unit-test result, pytest gate result, docs/CHANGELOG updated, `CI: ⏭️ not configured`, and any escalation (e.g. all identities stale — an environment issue, not code).

## Rules

- Never end without a `CHANGELOG.md` entry.
- Never modify infra files (`.claude/rules/*` except learned-lessons, `.claude/agents/*`, `.claude/commands/*`, `.claude/workflows/*`).
- Never revert working-tree files.
- Never commit or print `mobile/identities.json`.
- Skip rebase and CI-watch (targets: rebase none, CI none).
