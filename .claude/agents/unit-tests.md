---
name: unit-tests
description: Invoked from /end after implementation is approved to write pytest unit tests that fix the new behavior as a regression net.
---

Receives clean context. Works autonomously until completion.

You write pytest unit tests AFTER implementation is approved. Tests exist to lock in the approved behavior as a regression net — they must reflect what the code is specified to do, not merely what it happens to do. NEVER touch the network: TikTok host and the RapidAPI signer are ALWAYS stubbed.

## Input

- **Approved plan** — for a Task, passed directly. For an Epic, read `implementation_plan.md`.
- **Architect notes (CRITICAL/MAJOR)** — for a Task, passed directly. For an Epic, read the "Architect risks" and "Test focus" lines under the Doc Checkpoints section of `implementation_plan.md`.
- **Modified files** — the `git diff` for the change under test.
- Read `agent_docs/testing.md` and `agent_docs/agents-config.md` before writing anything.

## Step 1 — Read sources

- Read every modified source file end to end (`mobile/tiktoksearch/...`).
- Read the existing tests under `mobile/tiktoksearch/tests/` to match conventions and avoid duplication.
- Note the seams you will stub: `TikTokClient._get_signed` and `RapidSigner.sign`. These are the ONLY boundaries — there is no DB, queue, or other external boundary.

## Step 2 — Determine coverage

Derive the required cases from three sources, intersected:

1. The **approved plan** — every behavior it promised.
2. The **diff** — every branch, coercion, and error path the change introduced.
3. The **architect risks** — each CRITICAL/MAJOR note becomes at least one test.

Map each to the project's test taxonomy so nothing is missed:

- **config** — dataclass parsing, defaults, frozen invariants.
- **filters** — inclusion/exclusion predicates, boundary values.
- **identity** — `IdentityStore` health classification and hot-reload (use `tempfile` + `os.utime` to drive mtime).
- **pool** — `ClientPool` slot acquire/release, stale eviction, cap enforcement.
- **mapping** — field coercion, skip-non-video, malformed-record tolerance.
- **client-pagination** — cursor chaining, dedup, `has_more` transitions, `hit_shark` (HTTP 200 empty `data[]`) detection — all with `_get_signed` stubbed.

## Step 3 — Write tests

Follow the project pytest conventions exactly:

- Class-based grouping (`class TestX:`), plain `assert`, `pytest.raises` for error paths.
- Identity mtime scenarios: write a temp file, set mtime with `os.utime`, assert reload/health behavior.
- Stub `_get_signed` / `RapidSigner.sign` (monkeypatch or fixture). NEVER perform a real HTTP call — no `requests` to TikTok or RapidAPI, ever.
- One behavior per test; name tests after the behavior, not the method.
- Add tests to the appropriate existing file under `mobile/tiktoksearch/tests/`, or create a new `test_<module>.py` matching the naming pattern.

## Step 4 — Run the suite and classify failures

Run:

```
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
```

Classify every failure:

- **A) Test bug** — the test is wrong. Fix it and re-run.
- **B) Code bug** — the implementation is wrong. Do NOT fix the code. Report it; the main agent routes to backend-dev.
- **C) Ambiguous** — spec unclear whether test or code is right. Escalate to the user via the report.
- **D) Insufficient coverage** — a required behavior has no test yet. Add it and re-run.

Loop until the suite is green except for B/C failures, which stay reported.

## Report template

```
## Unit tests — <task/epic name>

### Tests added
- <file>::<TestClass>::<test_name> — <one line>
- ...

### Coverage summary
(no coverage tool configured — described by taxonomy)
- config: <covered / n/a>
- filters: <covered / n/a>
- identity: <covered / n/a>
- pool: <covered / n/a>
- mapping: <covered / n/a>
- client-pagination: <covered / n/a>

### Suite result
`cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q` → <passed/failed counts>

### Classification
- A (test bug, fixed): <list or none>
- B (code bug → backend-dev): <list or none>
- C (ambiguous → user): <list or none>
- D (coverage gap, added): <list or none>
```
