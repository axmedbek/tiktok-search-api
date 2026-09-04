# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Claude Code dev-flow infrastructure: `CLAUDE.md`, `agent_docs/` (architecture, testing, agents-config, common-changes recipes), `.claude/agents/` (backend-dev, architect-review, spec-compliance-reviewer, api-verifier, unit-tests, integration-tests, test-case-writer), `.claude/commands/` (start, dev, end, verify, debug, onboard, onboard_setup), `.claude/rules/` (code-standards, security, anti-block, api-service, git-workflow, communication, agent-docs-standards, learned-lessons), and `.claude/workflows/06-task-execution-protocol.md`.
- `IdentityStore` hot-reloadable warm-identity manager with per-identity health tracking (`identity_manager.py`); pool integration; `capture_identity_addon.py` mitmproxy addon for credential refresh.

### Changed
- `client.py` hardened `hit_shark` detection: empty `data[]` with `has_more=false` is now surfaced as `SoftError` (HTTP 502) instead of a silent empty 200.
- `api/app.py` `_resolve_identities_path` resolves a relative `identities_path` against the config directory (not process cwd).
