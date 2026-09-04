# Project Analysis & Report

**READ ONLY. Zero file changes.** Discover the project, present an interim report, ask the language question then a short set of project questions, and hand off to `/onboard_setup`.

## Discover (silent)

- `git ls-files | head -200` and a shallow tree of the repo (skip `.venv/`).
- Config files: `requirements.txt`, `pyproject.toml` (if any), `mobile/config_*.yaml`.
- Existing infra: `.claude/` (agents, rules, commands, workflows) and `agent_docs/`.
- Python surface: count `.py` files, list the `mobile/tiktoksearch/` modules.

## Deep-analyze (silent)

- `requirements.txt` / `pyproject.toml` — dependencies (FastAPI, Uvicorn, requests, PyYAML, pycryptodome, gmssl, pytest).
- `mobile/tiktoksearch/` modules — especially `api/app.py`, `api/schemas.py`, `client.py`, `mapping.py`, `config.py`, `errors.py`, identity/pool/signer modules.
- `mobile/config_*.yaml` — the three signer profiles (direct / working / signed).
- Tests under `mobile/tiktoksearch/tests/`.
- Git workflow: base = **main**, **no CI**, **no Conventional Commits** (freeform history).

## Interim report (present to user)

- **Stack / type:** Python 3.11 HTTP API **service for signed TikTok mobile search** (FastAPI + Uvicorn + requests). No web UI.
- **Architecture:** `POST /search` / `GET /health` → ClientPool → TikTokClient (signs via RapidSigner/Metasec) → TikTok direct API → `mapping.py` → `SearchPage`. Central problem: defeating `hit_shark` risk-control.
- **Scale:** file/module counts, LOC ballpark.
- **Key modules:** the module table from `architecture.md`.
- **Key patterns:** frozen-dataclass config, three YAML signer profiles, warm identities + health/reload, empty-in-direct-mode = `SoftError` (HTTP 502), dedup pagination.
- **Git workflow:** base main, no CI, no Conventional Commits.
- **Could-not-determine:** list anything ambiguous.

## Questions

Ask the **communication language question first** (mandatory), then **≤ 5** project questions, e.g.:
1. Deployment target (local + cloudflared tunnel only, or a hosted box / container)?
2. Proxy provider for residential IPs (which vendor, per-identity binding)?
3. How are warm identities sourced (emulator capture loop, manual, purchased)?
4. Which signer profile is canonical for production (`config_direct.yaml` tiktanic vs `working`)?
5. Any secrets/paths beyond `mobile/identities.json` that must never be committed/printed?

## Handoff

Tell the user to run **`/onboard_setup`** to write/refresh the infrastructure from these answers.

## Rules

- Read-only: create/modify **no** files.
- Python HTTP-API service — no Shopify/web/UI detection.
- Never read or print `mobile/identities.json`.
