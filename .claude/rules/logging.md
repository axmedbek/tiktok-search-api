# Logging

- Use the per-module `logging.getLogger('tiktoksearch.<module>')` loggers. Never bare `print()`.
- Log enough to diagnose: attempt number, `device_id`, path, error class.
- NEVER log secrets: no cookie, `x_tt_token`, `sessionid`, full auth headers, RapidAPI key, or proxy credentials. Never log `identities.json` contents.
- When a secret value must appear, mask it as `first6…last4`.
- hit_shark empties log at WARNING with device + path. Keep the canonical shape: `empty result (attempt N, device=..., path=...)`.
