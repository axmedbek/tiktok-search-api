---
paths:
  - "mobile/tiktoksearch/**"
  - "mobile/*.py"
---

# Lessons — Anti-block / Signer

Domain lessons for the risk-control and signing surface. Loaded only when files under `mobile/` are in play. Format per `.claude/rules/agent-docs-standards.md`.

- **`data[]` items map to empty records (all fields None).** The search response `data[]` is mixed — most items are videos (`type=1`, wrapped in `aweme_info`, has `aweme_id`) but some are user cards / ads with no `aweme_id`. **Fix:** `mapping.flatten_video` returns `None` for any item lacking `aweme_id`, and `client.unwrap` skips them; never assume `data[0]` is a video.

- **Swapping the RapidAPI signer provider does not fix empty results.** The RapidAPI signer returns a valid 200 with real `x-argus`/`x-gorgon`; empties come from identity distrust, not the signature. **Fix:** provider switching (`tiktanic` ↔ `working`) is for quota exhaustion only; empty results are resolved by refreshing warm identities, not by changing the signer.
