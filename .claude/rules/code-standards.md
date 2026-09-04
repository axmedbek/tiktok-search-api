# Code Standards

Python 3.11. Terse, correct, boring code.

## Imports
- `from __future__ import annotations` first, before any other import (the codebase does this).
- Group in order, blank line between groups: stdlib → third-party (`fastapi`, `requests`, `pydantic`, `yaml`) → internal absolute (`tiktoksearch.*`) → local relative (`.errors`, `.config`).

## Contracts & boundaries
- Type-hint every public function/method signature.
- Validate external data at its boundary, never deep in the stack:
  - HTTP input → Pydantic schemas in `api/schemas.py`.
  - YAML config → `config.py` dataclasses via `from_mapping` (coerce/validate there).
- ❌ Never pass raw `dict` from an external source into `client.py`/`pool.py`; convert to a validated type first.

## Error handling
- Catch to add context or recover, then re-raise — ❌ never swallow.
- Raise the domain exceptions in `errors.py` (`RateLimited`, `SoftError`, `TransportError`, `PoolExhausted`); do not invent parallel ad-hoc exceptions.
- ❌ Never expose internal detail to an HTTP client: no stack traces, cookies, `x_tt_token`, `sessionid`, device ids, proxy URLs in a response or exception message.

## Functions
- One responsibility. Guard clauses over nested `if`. Delete dead code rather than commenting it out.

## Discipline
- No `TODO` without a tracker reference. No commented-out code. No leftover `print()` — use the per-module logger already set up (e.g. `logging.getLogger('tiktoksearch.client')`).
- No magic numbers/strings — name them or source from config.

## Frozen-dataclass discipline
- `config.py` objects are `frozen`/immutable. Mutate only via `dataclasses.replace()` or `with_overrides`. ❌ Never monkeypatch attributes onto a config instance.
