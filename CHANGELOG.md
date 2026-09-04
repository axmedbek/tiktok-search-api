# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Claude Code dev-flow infrastructure: `CLAUDE.md`, `agent_docs/` (architecture, testing, agents-config, common-changes recipes), `.claude/agents/` (backend-dev, architect-review, spec-compliance-reviewer, api-verifier, unit-tests, integration-tests, test-case-writer), `.claude/commands/` (start, dev, end, verify, debug, onboard, onboard_setup), `.claude/rules/` (code-standards, security, anti-block, api-service, git-workflow, communication, agent-docs-standards, learned-lessons), and `.claude/workflows/06-task-execution-protocol.md`.
- `IdentityStore` hot-reloadable warm-identity manager with per-identity health tracking (`identity_manager.py`); pool integration; `capture_identity_addon.py` mitmproxy addon for credential refresh.
- Docker deployment: `Dockerfile` (`python:3.11-slim`, non-root, stdlib-urllib healthcheck) and `docker-compose.yml` running the API on `127.0.0.1:8000` plus `mobile/demo.html` via `nginx:alpine` on `127.0.0.1:8080`. Config and warm identities are bind-mounted read-only from `./mobile`; `.dockerignore` keeps every profile and `identities.json*` out of the build context.
- `RAPIDAPI_KEY` environment override for the signer key (`RAPIDAPI_KEY_ENV` in `config.py`), so the key no longer has to live in a committed profile. `.env.example` documents it; `.env` is git-ignored.
- Startup misconfiguration banners in `create_app`: `ERROR` when the config path is absent, and when no signer key is configured — a falsy `rapidapi_key` silently selects the cold legacy signer and disables hit_shark detection, which would otherwise return a silent `200` with `count: 0`.

### Changed
- `client.py` hardened `hit_shark` detection: empty `data[]` with `has_more=false` is now surfaced as `SoftError` (HTTP 502) instead of a silent empty 200.
- `api/app.py` `_resolve_identities_path` resolves a relative `identities_path` against the config directory (not process cwd).
- `mobile/config_direct.yaml` no longer carries a literal `rapidapi_key` — it now comes from `RAPIDAPI_KEY`. **The previously committed key remains in git history and should be treated as compromised and rotated.**
