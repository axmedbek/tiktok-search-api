# Changelog

All notable changes to this project are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Claude Code dev-flow infrastructure: `CLAUDE.md`, `agent_docs/` (architecture, testing, agents-config, common-changes recipes), `.claude/agents/` (backend-dev, architect-review, spec-compliance-reviewer, api-verifier, unit-tests, integration-tests, test-case-writer), `.claude/commands/` (start, dev, end, verify, debug, onboard, onboard_setup), `.claude/rules/` (code-standards, security, anti-block, api-service, git-workflow, communication, agent-docs-standards, learned-lessons), and `.claude/workflows/06-task-execution-protocol.md`.
- `IdentityStore` hot-reloadable warm-identity manager with per-identity health tracking (`identity_manager.py`); pool integration; `capture_identity_addon.py` mitmproxy addon for credential refresh.
- Docker deployment: `Dockerfile` (`python:3.11-slim`, non-root, stdlib-urllib healthcheck) and `docker-compose.yml` running the API on `127.0.0.1:8000` plus `mobile/demo.html` via `nginx:alpine` on `127.0.0.1:8080`. Config and warm identities are bind-mounted read-only from `./mobile`; `.dockerignore` keeps every profile and `identities.json*` out of the build context.
- `RAPIDAPI_KEY` environment override for the signer key (`RAPIDAPI_KEY_ENV` in `config.py`), so the key no longer has to live in a committed profile. `.env.example` documents it; `.env` is git-ignored.
- Cross-request pagination via `page_token` (`paging.py`). TikTok's search is a session: a request at `offset > 0` must echo the previous reply's `search_id` (`log_pb.impr_id`) or the API answers `empty_session` and returns nothing — so `next_cursor` was never usable and the demo's "load more" always 502'd. The token is an HMAC-authenticated blob carrying, per endpoint, `(path, cursor, search_id, has_more, started)`, an `hmac`-derived device handle, and a bounded window of recent record-id fingerprints that seeds cross-page dedup. Verified live: three chained pages, 30 distinct ids, zero overlap. `cursor` stays accepted and unchanged for compatibility.
- `PoolCode` on `PoolExhausted`, so `app.py` maps 429-vs-503 on an enum instead of matching the prose substring `'cap reached'`.
- Startup misconfiguration banners in `create_app`: `ERROR` when the config path is absent, and when no signer key is configured — a falsy `rapidapi_key` silently selects the cold legacy signer and disables hit_shark detection, which would otherwise return a silent `200` with `count: 0`.

### Changed
- `client.py` hardened `hit_shark` detection: empty `data[]` with `has_more=false` is now surfaced as `SoftError` (HTTP 502) instead of a silent empty 200.
- `api/app.py` `_resolve_identities_path` resolves a relative `identities_path` against the config directory (not process cwd).
- `next_cursor` is now TikTok's own cursor rather than `start_cursor + len(records)`; the two diverge whenever dedup drops an item. It is informational — callers resume with `page_token`.
- A continuation whose reply is empty is the end of the stream, not risk-control: it returns `200 {count: 0, has_more: false, page_token: null}` in one signed request and is not counted against identity health. Previously every terminal "load more" raised `SoftError`, and three of them retired the sole warm identity and 503'd all callers. The tail is an allow-list (`empty_session`, `federation_empty`, or an empty page with no nil block) that fails closed — `hit_shark` or an unrecognised nil still raises, so a session cannot launder risk-control — and it retires that endpoint regardless of the `has_more` the reply carries.
- The device serving a paginated stream is pinned by a stable `hmac(identity_key)` handle rather than the positional `id{i}` label, which a capture-loop reorder of `identities.json` would repoint at a different device.
- A user search whose page answers `has_more=false` is no longer misread as a shadow-block — `items_any` now covers `user_list`. Pre-existing bug, surfaced because token pagination reaches the final user page routinely.
- `mobile/config_direct.yaml` no longer carries a literal `rapidapi_key` — it now comes from `RAPIDAPI_KEY`. **The previously committed key remains in git history and should be treated as compromised and rotated.**
