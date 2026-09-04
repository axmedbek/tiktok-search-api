# Agents Config — Project Norms

Single source of project-specific commands, versions, and conventions. Every agent and command reads this file. Keep it accurate — stale entries here cause wrong decisions in every future session.

## Stack

- **Language:** Python 3.11 (uses `dataclass(slots=True)` and `X | None` unions — 3.10+ required; do not use syntax that breaks on 3.11)
- **Web framework:** FastAPI + Uvicorn (ASGI). App factory: `mobile/tiktoksearch/api/app.py` `create_app(config_path)`.
- **HTTP client:** `requests` (with `requests[socks]` for SOCKS proxies)
- **Config:** YAML (`PyYAML`) → frozen `dataclass` (`config.py`). Config is immutable; overrides via `replace()`.
- **Crypto (vendored signer):** `pycryptodome`, `gmssl` (used by `tiktok_signer/` — the vendored pure-Python signer, largely superseded by the RapidAPI signer)
- **Domain:** TikTok mobile search via signed direct-API requests. No official API — this is a reverse-engineering / anti-bot-evasion problem. See `architecture.md`.

## Commands

- **Virtualenv:** `.venv/` at repo root. Always invoke as `.venv/bin/python` (repo root) or `../.venv/bin/python` (from `mobile/`). The system `python3`/`python` may be missing `yaml`/`requests`/`gmssl` — `import tiktoksearch` pulls the vendored signer chain and fails without `gmssl`, so isolate `config.py` with `importlib` (registering the module in `sys.modules` first, required for `slots=True` dataclasses) when testing config outside the venv. On this machine the venv is Python 3.12 and `ensurepip` is unavailable, so it was bootstrapped with `get-pip.py`; the Docker image pins 3.11.
- **Run tests:** `cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q`
- **Run the API (dev):** `.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000`
- **Run via Docker:** `cp .env.example .env` (set `RAPIDAPI_KEY`), then `docker compose up -d --build`. API on `127.0.0.1:8000`, demo UI on `127.0.0.1:8080`. Logs: `docker compose logs -f api`. Stop: `docker compose down`.
- **Public tunnel (dev):** `cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate`
- **Lint:** none configured. Code review is the only style gate — follow `.claude/rules/code-standards.md`.
- **Type checker:** none configured (no mypy/pyright). Type hints are documentation, not enforced.
- **Test prereq:** none — tests are pure unit tests, no DB or network.

## Test framework

- **pytest**, tests live in `mobile/tiktoksearch/tests/`. Class-based grouping (`class TestX:`), plain `assert`, `pytest.raises` for error cases.
- Network- and emulator-dependent code (client search, RapidAPI signer, capture addon) is NOT covered by the automated suite — those require live identities/proxies/emulator. Test pure logic (config parsing, filters, identity health/reload, mapping, pagination dedup) with fakes; never hit TikTok or RapidAPI in a unit test.

## Config files (three signer profiles)

- `mobile/config_direct.yaml` — **primary.** RapidAPI `tiktanic` signer + warm identities + IdentityStore (`identities_path`). This is the working direct-API path.
- `mobile/config_working.yaml` — RapidAPI `working` signer (alternate provider, switch when tiktanic quota exhausted).
- `mobile/config_signed.yaml` — vendored MetasecSigner + synthetic devices. Cold/legacy; returns empty (v46 risk-control rejects v37-era argus). Do not use for real results.

## File-size norm

- Keep modules focused. Existing modules are ~50–200 lines. If a module crosses ~300 lines, consider splitting by responsibility. Not a hard gate — cohesion matters more than line count.

## Files not to touch without explicit request

- `mobile/identities.json` — contains live cookie/x-tt-token secrets, git-ignored. Never commit; never print its contents in logs or reports.
- `mobile/tiktoksearch/tiktok_signer/**` — vendored third-party signer. Modify only when the task is specifically about the vendored signer.

## Git Workflow

- **Base branch:** main
- **Rebase target:** none (single-branch repo, no long-lived integration branch)
- **MR target:** main
- **CI platform:** (none configured)
- **Conventional Commits:** no (recent history is freeform, e.g. "search more"). Use short imperative subjects.
- **Branch prefixes:** feat/, fix/, refactor/, test/, docs/, chore/

## Browser Testing

- **Not applicable in the usual sense.** There is no web UI to drive with Playwright. `demo.html` is a static HTML client, not a build target.
- "User-visible behavior" here = the **HTTP API responses** (`POST /search`, `GET /health`). Verify those with `curl` against `http://127.0.0.1:8000` (or the public tunnel URL), not a browser. See `.claude/agents/api-verifier.md`.
