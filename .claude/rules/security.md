# Security

## Secrets — NEVER
- ❌ Commit `.env` or any config carrying real keys. `mobile/identities.json` is git-ignored — keep it that way.
- ❌ Log, `print()`, or `echo` a cookie, `x_tt_token`, `sessionid`, or a full auth header.
- ❌ Return any of the above in an HTTP response body or in an exception/error message.
- ❌ Echo the contents of `identities.json` anywhere (logs, responses, test output).
- ✅ Mask any secret that must appear in diagnostics: `first6…last4`.

## Input validation
- Validate at the API boundary with Pydantic (`api/schemas.py`); coerce config safely in `config.py`.
- ❌ Never build URLs/query strings by concatenating untrusted input. Use `urllib.parse.urlencode` as `client.py` does.

## Proxies & TLS
- Proxy credentials inside proxy URLs are secrets — mask them in logs via `pool.py`'s `_mask_proxy`. ❌ Never log a raw proxy URL.
- ❌ Never weaken TLS verification (no `verify=False`) to work around an error.
