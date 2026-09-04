# Git Workflow

Config source: `agent_docs/agents-config.md` → `## Git Workflow`.

- Base branch: `main`. Rebase target: **none** — skip rebase entirely.
- No Conventional Commits: short imperative subjects, ≤50 chars.
- No CI configured: skip any CI-watch step.
- Branch prefixes: `feat/` `fix/` `refactor/` `test/` `docs/` `chore/`.
- Force-push only with `--force-with-lease`. ❌ Never plain `--force`.
- ❌ Never commit `identities.json` or any real secret/key.
- ❌ Never push directly to `main` for substantive work — branch first.
