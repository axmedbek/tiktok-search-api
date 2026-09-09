---
paths:
  - "mobile/tiktoksearch/mapping.py"
---

# Lessons — Payload mapping

Domain lessons for turning raw TikTok payloads into client-facing records. Loaded only when `mapping.py` is in play. Format per `.claude/rules/agent-docs-standards.md`.

- **A boolean record field is inverted — a public account is reported `private: true`, with no error and no log.** The field was coerced with a bare `bool()`, but TikTok's `secret` is a numeric 0/1 flag that a reply may render as the string `'0'`, and `bool('0')` is `True`. Nothing raises: the wrong answer is well-formed, so it reaches the client and `demo.html` renders a "private" badge on an open account. **Fix:** every numeric 0/1 flag goes through `mapping._flag`, which coerces with `to_int` and compares numerically, falling back to truthiness only for a non-numeric value — where it errs toward `True`, the conservative direction for a privacy flag. Truthiness is reserved for `mapping._verified`'s `custom_verify` / `enterprise_verify_reason`, which are string-or-absent fields where a non-empty string genuinely *is* the semantic.

- **A renamed or absent presentation field (`total_favorited`, `avatar_larger`) yields `None` or a lower-resolution URL rather than an error, and nothing reports it.** That silent degradation is deliberate, not an oversight. **Fix:** the flatteners degrade to `None` for presentation fields and raise nothing, because an exception from the flattening layer would surface through the same `SoftError` → 502 channel as risk-control and get triaged as `hit_shark` — turning a payload-shape change into a phantom identity problem. Only load-bearing fields gate a record: a missing `aweme_id` / `uid` returns `None`, and `sec_uid` is extracted explicitly because the user-scoped endpoints cannot be called without it.

- **A "filter by author" returns another account's video, and reports that account's ids as the requested user's.** The filter compared `author_username`, which `flatten_video` fills as `author.get('unique_id') or author.get('nickname')`. A nickname is **user-settable and not unique**, so anyone can set theirs to a target handle and be attributed to it; the id provenance then read its ids off the first kept record and published a foreign identity. Nothing errored — the answer was well-formed and wrong. **Fix:** `flatten_video` carries `author_unique_id`, the raw `unique_id` with NO nickname fallback, and every identity comparison uses that; `author_username` stays the display field. A record without `author_unique_id` is dropped, never matched. The two keys are asserted in the same test — separately, a later "harmonisation" of them passes silently and reintroduces this.
