# Dependencies

- Dependencies live in `requirements.txt`: `requests[socks]`, `pycryptodome`, `gmssl`, `PyYAML`, `fastapi`, `uvicorn`, `pydantic>=2`.
- Adding a dependency is an architectural decision — justify it, prefer stdlib, and only add when a real need exists. An unjustified new dep is an architect-review CRITICAL.
- Pin `pydantic>=2` (v2 API).
- The vendored `tiktok_signer/` is third-party and superseded by `RapidSigner` for v46 — don't add deps to satisfy it.
- Never add a dep that weakens TLS or bundles secrets. Keep the `socks` extra for socks5 proxies.
