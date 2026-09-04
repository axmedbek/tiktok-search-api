# CLAUDE.md — tiktok-searcher

TikTok mobile-search-as-a-service. Signed direct-API requests reproduce TikTok's mobile search to return real paginated results without a phone or official API. The hard problem is **defeating ByteDance risk-control (`hit_shark`)** — an identity/anti-bot problem, not CRUD.

## Golden rules

1. **The signer is rarely the culprit.** Empty results (HTTP 200, no `data[]`) are almost always `hit_shark` — expired warm cookie/token, cold device, or bad IP. Diagnose identity before touching signing. See `agent_docs/architecture.md` § hit_shark.
2. **Never commit or print secrets.** `mobile/identities.json` holds live `cookie`/`x_tt_token`. It is git-ignored; keep it that way; never echo its contents into logs, reports, or the terminal.
3. **Never hit TikTok or RapidAPI from a unit test.** Stub at the `_get_signed` / `RapidSigner.sign` boundary. Live paths are verified manually.
4. **Use the venv.** `.venv/bin/python` (repo root) — the system Python may be older or missing deps.
5. **`config_direct.yaml` is the working profile.** `config_signed.yaml` is the cold legacy path (returns empty by design).

## Dev flow

This project uses a plan → execute → finalize workflow. Commands live in `.claude/commands/`:

- **`/start`** — research, classify (Spike/Task/Epic), plan, confirm. Writes no code.
- **`/spike`** — reverse-engineering investigation for open questions where the outcome is knowledge, not code (why empty, is the signer stale, did the endpoint change). Delegates to the `researcher` agent; ends by handing any warranted change to `/start`.
- **`/dev`** — execute the approved plan through the quality pipeline (delegate → review → test → docs). Orchestrates subagents; the main agent does not write production code.
- **`/end`** — finalize: hygiene, unit tests, docs audit, CHANGELOG, commit, push.
- **`/onboard`** → **`/onboard_setup`** — (re)generate project docs + infra for a fresh clone.
- **`/verify`**, **`/debug`** — API smoke-check / bug triage.

**Spike vs build:** if you know what code to change → `/start`. If you must first find out what's true about TikTok/the signer/risk-control → `/spike`. See `.claude/rules/research-discipline.md`.

Full pipeline contract: `.claude/workflows/06-task-execution-protocol.md`.

## Project memory

- `agent_docs/` — architecture, testing, config norms, common-changes recipes. Read before planning; update in `/dev` Phase 4 and `/end`.
- `.claude/rules/` — mandatory standards (code, security, git, anti-block, api-service) + `learned-lessons.md`.
- Assistant auto-memory (outside the repo) holds the deep reverse-engineering history (hit_shark diagnosis, direct-api-works, identity auto-refresh, serving-public-url). Trust `agent_docs/` for current code facts; treat memory as background.

## Communication

Code, comments, and docs are always in English. Conversation language follows `CLAUDE.local.md` → `## Communication Language` (default English).
