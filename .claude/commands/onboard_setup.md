# Write Infrastructure (Part 2)

Take the answers from `/onboard` and write/refresh the project's Claude infrastructure. In this repo the infra **already exists**, so treat this as an **idempotent refresh**: update stale sections in place, create only what is missing, and never clobber learned lessons.

## Preconditions

- `/onboard` was run this session and the user answered the language + project questions.
- If run cold, first do the `/onboard` discovery silently, then proceed.

## What to write / refresh

**`CLAUDE.local.md`** — ensure a `## Communication Language` section with the user's chosen language. Create the file if absent; otherwise update only that section.

**`CLAUDE.md`** — top-level project brief: stack (Python 3.11, FastAPI/Uvicorn/requests), the run/test commands, the `hit_shark` central problem, "no web UI — API is verified with curl", and the `mobile/identities.json` secret rule. Refresh stale lines only.

**`agent_docs/`** (refresh in place per `.claude/rules/agent-docs-standards.md` — REPLACE don't append; no date stamps):
- `agents-config.md` — Stack, Commands, Config files, Git Workflow, Browser Testing (= curl). Fold in deployment target / canonical signer profile answers.
- `architecture.md` — module table, request flow, `hit_shark` section, SoftError→502 contract.
- `testing.md` — pytest command, what is unit-tested vs live-only.
- `common-changes.md` (index) + `common-changes-api.md` + `common-changes-identity.md` — fold in proxy-provider and identity-sourcing answers.

**`.claude/rules/*`** — update `anti-block.md`, `api-service.md`, `code-standards.md`, `communication.md`, `git-workflow.md`, `security.md` only where the answers changed a norm. **Never** overwrite `.claude/rules/learned-lessons.md` or `.claude/rules/lessons/*` — those accumulate and are appended-to only via the lesson format.

## Idempotency rules

- REPLACE stale sections in place; do not stack new blocks below old content.
- Create only files that are genuinely missing.
- Never clobber `learned-lessons.md` / `lessons/*`.
- No status headers, no "verified on <date>" stamps.
- Never write real secrets — reference `mobile/identities.json` by path only.

## Confirm

Report exactly which files were **created** vs **refreshed** (and which sections), and confirm the communication language is recorded in `CLAUDE.local.md`.
