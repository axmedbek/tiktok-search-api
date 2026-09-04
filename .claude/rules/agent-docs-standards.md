---
paths:
  - "agent_docs/**"
  - ".claude/rules/lessons/**"
  - ".claude/rules/learned-lessons.md"
---

# Agent-Docs & Learned-Lessons Standards

Applies to `agent_docs/*`, `.claude/rules/lessons/*`, and `.claude/rules/learned-lessons.md`.

## Editing style
- REPLACE, don't APPEND. Update the stale section in place — ❌ never stack a new append block below old content.
- ❌ No status headers, no "verified on <date>" stamps, no `M*N*`-style run tags.

## Learned-lesson format
```
- **<Symptom>.** <Cause>. **Fix:** <the standing invariant, phrased as something that IS true>
```
- The **Fix** states an invariant that holds — ❌ never phrase it as a to-do or imperative.

## Reconciling duplicates
- Before adding a lesson, grep by IDENTIFIERS (symbols / file paths / env vars / error strings), NOT by prose.
- If a lesson about the same defect exists, update/merge it. ❌ Do not add a parallel lesson for the same defect.

## Where a lesson lives
- Domain-specific → `.claude/rules/lessons/<domain>.md` with its own `paths:` frontmatter.
- Unbound / process lesson → `.claude/rules/learned-lessons.md` (unconditional, no frontmatter).

## Corpus ceilings
- Keep unconditional context (`CLAUDE.md` + all non-path-scoped rules) under ~10000 tokens.
- Split a thematic lessons group once it passes ~25 lessons.

## Anti-bloat checklist (run before saving)
- [ ] No Fix phrased as a to-do — it states an invariant.
- [ ] No ticket number used as attribution.
- [ ] No positional references ("the rule above", "as mentioned below") — name the thing.
- [ ] Backticks intact and balanced around every symbol/path.
- [ ] No duplicate of an existing lesson for the same defect.
