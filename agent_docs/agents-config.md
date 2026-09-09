# Agents Config — Project Norms

Single source of project-specific commands, versions, and conventions. Every agent and command reads this file. Keep it accurate — stale entries here cause wrong decisions in every future session.

## Stack

- **Language:** Python 3.11 (uses `dataclass(slots=True)` and `X | None` unions — 3.10+ required; do not use syntax that breaks on 3.11)
- **Web framework:** FastAPI + Uvicorn (ASGI). App factory: `mobile/tiktoksearch/api/app.py` `create_app(config_path)`.
- **HTTP client:** `requests` (with `requests[socks]` for SOCKS proxies)
- **Config:** YAML (`PyYAML`) → frozen `dataclass` (`config.py`). Config is immutable; overrides via `replace()`.
- **Crypto (vendored signer):** `pycryptodome`, `gmssl` (used by `tiktok_signer/` — the vendored pure-Python signer, which is the **default** signer via `signer: local`; it signs v46 in-process at zero quota)
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
- Network- and emulator-dependent code (the live client search, the RapidAPI signer, the capture addon) is NOT covered by the automated suite — it needs live identities/proxies/emulator. Test pure logic (config parsing, filters, identity health/reload, mapping, pagination dedup, token round-trips) with fakes; never hit TikTok or RapidAPI in a unit test.
- `tests/conftest.py` installs a session-scoped `HTTPAdapter.send` tripwire that **fails the run** if any test reaches the network, so a test can never spend signer quota. The local signer's crypto runs in-process, so `signer: local` paths are testable without stubbing the signature itself. `conftest.py` is the ONLY stubbing mechanism — its fake roster is in `testing.md`, and adding a parallel one is a review finding.
- **A green suite is not evidence on its own.** Nine mutations to load-bearing rules survived a fully green 497-test run in one session — including `NEUTRAL → True` (which would stop a dying identity ever being retired) and dropping `reverse=True` (which would invert every post feed). For any change to classification, ordering, or health-verdict logic: mutate the production rule, confirm the new test fails, restore, verify by checksum, and report the table. `testing.md` records the assertion shapes that look fine and prove nothing.

## Config files (three signer profiles)

- `mobile/config_direct.yaml` — **primary.** `signer: local` (vendored signer, zero quota) + warm identities + IdentityStore (`identities_path`). This is the working direct-API path.
- `mobile/config_working.yaml` — no `signer:`, carries a key → derives `rapid` with the `working` provider (alternate paid provider; switch when tiktanic quota is exhausted).
- `mobile/config_signed.yaml` — no `signer:`, no key → derives `legacy` + synthetic devices. Returns empty **because `legacy` signs with the v32 params and attaches no warm identity**, not because the vendored signer cannot do v46. Do not use for real results.

## The `signer:` knob (`ClientConfig.resolved_signer()`)

The signer and the direct-API path are separate concerns — `_direct` is no longer "has a RapidAPI key".

| `signer:` | Signer | Path | Quota |
|---|---|---|---|
| `local` | vendored, via `MetasecSigner.for_v46()` (maps `sign_*` onto the four fields `Metasec.sign` reads, attaches the warm identity) | direct | **free** |
| `rapid` | `RapidSigner` | direct | paid, 1+ per request |
| `legacy` | vendored on the v32 defaults, no identity | old cold path (`api_hosts`, `count=20`) | free, empty by design |
| *unset* | `rapid` if `rapidapi_key` else `legacy` | — | preserves pre-knob behaviour |

- `from_mapping` **rejects** an unknown non-empty value (a `signer: locl` typo would otherwise derive to `rapid` and bill every request). Startup logs the resolved mode **and** the raw value.
- The RapidAPI fallback fires only on a hard signer-level `TransportError` in `local` mode, never on an empty/`hit_shark` — see `.claude/rules/lessons/anti-block.md`.
- **A stale local sign key is silent and looks exactly like expired credentials.** Diagnose by flipping one profile to `signer: rapid` and re-running the same query; that single paid signature is the only separating signal.

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

- **No build target, and no browser driver that works here.** `demo.html` is a static HTML client. Headless Firefox is present but snap-confined and times out under instrumentation, so **CSS layout is not verifiable by measurement on this machine** — a visual change ships on reading plus the developer opening the page.
- **Its JavaScript logic IS verifiable, and that is where the bugs were.** Extract the inline `<script>` and drive it in QuickJS with a small DOM shim, fed real saved API responses. That approach caught two live defects the eye missed: `esc()` throwing on a non-zero number and not escaping quotes (an attribute-context hole), and FastAPI's array-shaped 422 `detail` rendering as `[object Object]` on every tab. Always include an XSS pass — every field the page interpolates is API-derived and user-controlled.
- "User-visible behavior" = the **HTTP API responses**: `POST /search`, `POST /profile`, `POST /user/posts`, `GET /health`. Verify with `curl` against `http://127.0.0.1:8000` (or the tunnel URL). See `.claude/agents/api-verifier.md`.
