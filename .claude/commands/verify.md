# Verify the API

Smoke-test the HTTP API against a locally-run server. Use when the user asks to confirm a change works or wants a quick API check. Prefer delegating to the `api-verifier` agent; if running inline, follow the same steps and the same failure-classification discipline.

There is **no browser UI**. "User-visible behavior" = the HTTP API (`POST /search`, `GET /health`), checked with `curl`.

## Step 0 — Ensure the server is up

```
curl -s -o /dev/null -w '%{http_code}' localhost:8000/health
```
If not reachable, start it (background) and wait for `/health` to answer:
```
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000
```

## Step 1 — Health

```
curl -s localhost:8000/health
```
Check the **usable identity count**. If **0 usable / all stale**, stop and go to Classification — this is an environment problem, not a code bug.

## Step 2 — Search (first page)

```
curl -s -XPOST localhost:8000/search -H 'content-type: application/json' \
  -d '{"query":"ocean","type":"video","limit":10}'
```
Confirm the response has: `count` > 0, `has_more`, and each record carries **real** `description` + `author_username` (not empty/placeholder). Empty strings across all records = `hit_shark`, not a valid page.

## Step 3 — Pagination

Take `next_cursor` (and echoed `search_id` if present) from Step 2 and request page 2:
```
curl -s -XPOST localhost:8000/search -H 'content-type: application/json' \
  -d '{"query":"ocean","type":"video","limit":10,"cursor":"<next_cursor>"}'
```
Confirm the cursor advanced and new (deduped) records returned; `has_more` behaves correctly.

## Step 4 — Soft-error contract

Confirm an all-stale / all-empty condition surfaces as **HTTP 502 (SoftError)**, never a silent `200` with empty `data[]`. An empty result in direct mode is an error condition (see `architecture.md` § hit_shark).

## Classification (critical)

Every failure must be classified — do **not** blame code for an environment problem:

| Symptom | Likely cause | Action |
|---|---|---|
| `/health` shows 0 usable / all stale | expired warm cookie/`x_tt_token`, cold device | **Escalate to user** — refresh identities via capture loop. Not a code bug. |
| 200 with empty `description`/`author_username` on all records | `hit_shark` (creds/IP/device), or no residential proxy | Escalate; see `/debug`. Not necessarily code. |
| Response schema wrong / field missing / 500 / bad mapping | **code regression** | Report as a code bug (route to `backend-dev`). |
| 502 on all-stale | correct SoftError behavior | Pass (contract honored). |

## Rules

- Mask any secrets in output (never echo cookies / `x_tt_token` / `mobile/identities.json`).
- Distinguish **code regression** from **identity/environment** failure; escalate the latter to the user.
- Cross-refs: `.claude/agents/api-verifier.md`, `agent_docs/architecture.md`, `.claude/commands/debug.md`.
