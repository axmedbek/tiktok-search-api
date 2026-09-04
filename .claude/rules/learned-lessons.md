# Learned Lessons — Mandatory Rules

Rules from real failures on this project. NOT suggestions — requirements.
Format per `.claude/rules/agent-docs-standards.md`: `- **<Symptom>.** <Cause>. **Fix:** <the standing invariant, phrased as something that IS true>`.

## Risk-control / empty results

- **Search returns HTTP 200 with empty `data[]` ("arada boş" / "100/100 empty").** ByteDance `hit_shark` soft-rejects the request on identity distrust — expired warm `cookie`/`x_tt_token`, cold device, or bad IP — not a signing failure. **Fix:** an empty direct-mode result is diagnosed as an identity problem first (health/staleness, sessionid cookie + x_tt_token present, residential proxy assigned) and is surfaced as `SoftError` (HTTP 502), never a silent 200 with empty records.

## Environment

- **Server logs "identities file not found" and falls back to synthetic/static devices (health shows `dev0`, not `id0`).** A relative `identities_path` was resolved against the process cwd instead of the config's directory when the server was launched from the repo root. **Fix:** `api/app.py` `_resolve_identities_path` resolves a relative `identities_path` against the config file's directory, so the server finds `mobile/identities.json` regardless of launch cwd.
