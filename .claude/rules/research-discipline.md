# Research / Spike Discipline

This project's failures are usually about a changing external system (TikTok risk-control, signer scheme, endpoint), not about the code. Treat discovery as first-class work with its own rules.

- **Knowledge questions are Spikes, not Tasks.** If you cannot name the code change because you must first find out how TikTok/the signer/risk-control currently behaves, it is a `/spike`, not a `/start` plan. Do not force a research question into an implementation plan.
- **Identity-first reflex.** Before treating an empty/failed search as a code or signer bug, check `GET /health` for a usable identity. `usable: 0` (all stale) means the cause is expired credentials, not code — the fix is the capture loop, not a code change.
- **Every failure is triaged signer-vs-identity-vs-endpoint** before any conclusion. Signer = 403 / missing x-argus. Identity = 200 + empty `data[]` / `search_nil_info` (hit_shark). Endpoint-shape = 200 with data but the parser sees nothing (chunking/gzip/key/type change).
- **Probes are cheap and boxed.** One discriminating probe per hypothesis; ≤ 1 RapidAPI sign per hypothesis (quota is money); never loop-hammer TikTok. Inconclusive-within-box is an honest result — never manufacture a conclusion.
- **A spike never edits production code.** It ends by handing a concrete, planned change to `/start`, or by concluding there is nothing to build. Throwaway probes go in `/tmp` or `scratch-*.py` (deleted in `/end`).
- **Durable findings go to `agent_docs/`.** If the same question gets spiked twice, the answer belongs in `architecture.md` (§ hit_shark / § signer) so it is not re-investigated.
