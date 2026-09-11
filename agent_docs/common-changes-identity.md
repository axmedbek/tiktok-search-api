# Common Changes — Identity / Anti-block

## Add or rotate a warm identity
1. Fresh identities come from the capture loop (emulator + `capture_identity_addon.py` → `identities.json`). To add manually, append an object to `mobile/identities.json`:
   ```json
   {"device_id":"...","iid":"...","cookie":"...; sessionid=...","x_tt_token":"...","user_agent":"...","device_query":{...}}
   ```
2. The running API hot-reloads on file mtime change — no restart. Confirm via `GET /health` → `identities` block (`usable` count rises).
3. Never commit `identities.json` (git-ignored — holds live secrets).

## Add a proxy
- `config_direct.yaml` → `proxies:` list, one full URL per line: `scheme://user:pass@host:port` (`http` or `socks5`; socks5 needs `requests[socks]`, already in requirements).
- Assigned round-robin to identities/devices in `pool.py` `_build_slots`. Use **residential/mobile** proxies — datacenter IPs are blocked.

## Tune health thresholds
- `IdentityStore(path, stale_after=N)` — consecutive empty results before an identity is marked stale (`identity_manager.py` `DEFAULT_STALE_AFTER = 3`).
- Wire a config knob through `api/app.py` if it should be configurable per deployment.

## Change the capture flow
- `capture_identity_addon.py` runs under mitmproxy alongside the emulator, NOT inside the app. It only rewrites `identities.json` when creds actually change (avoids mtime churn).
- The hard blocker for capture is that ByteDance TTNet/Cronet ignores the system HTTP proxy (QUIC/UDP 443) — needs transparent proxy/VPN or a Frida Cronet hook. See project memory `emulator-traffic-setup-state`.
- **`capture_identity_addon.py` sees every request the app makes but records only identity material.** Recording the rest is a second, independent addon — see § Diff the app's params against ours.

## Diff the app's params against ours

`/aweme/v1/aweme/post/` and `/aweme/v1/user/profile/other/` are live handlers that reject this client's param set with a bodyless 200 and no feedback. Headers, encoding, host/region and the whole web route are already ruled out (`architecture.md` § data flow invariants), so the missing ingredient is a **query parameter**, and guessing its name is unbounded. `capture_requests_addon.py` + `tiktoksearch/capture_record.py` (the capture side) + `tiktoksearch/capture_diff.py` (the diff side) put the app's real query string beside ours instead.

1. Run it alongside the identity capture — two `-s` flags, one `mitmdump`, so one app session feeds both:
   ```
   cd mobile && mitmdump \
       -s capture_identity_addon.py --set identities_out=identities.json \
       -s capture_requests_addon.py --set capture_out=captured_requests.jsonl
   ```
   **The two addons need no project dependency; the diff CLI needs the venv.** That split is the operational rule: `mitmproxy` is deliberately **not** in `requirements.txt`, and `mitmdump` may be the apt/pipx one on a system interpreter — both addons still load there, because `capture_requests_addon.py` imports only `tiktoksearch/capture_record.py`, which is stdlib-only (it puts `mobile/tiktoksearch` on `sys.path` and imports `capture_record` as a top-level module, so `tiktoksearch/__init__.py` — which imports `.client` eagerly — never runs). Step 3's `capture_diff` is the half that needs `../.venv/bin/python`: it imports `.client` for the param builders, and that pulls `requests`, `pycryptodome`, `gmssl`, `PyYAML`.
   Measured, and the reason the split exists: while the addon imported `capture_diff`, an apt `mitmdump` printed `in script mobile/capture_requests_addon.py: No module named 'gmssl'`, **came up anyway with the addon not loaded**, and a capture session would have recorded nothing while looking healthy. Keep the addon's imports on `capture_record` only.
   `capture_paths` narrows what is logged (default: all `/aweme/v1/*`).
2. In the app: open a profile, then **scroll the post grid** past the first page. The grid's second page is what reveals a cursor param a first-page-only capture hides.
3. Read the diff (**from the venv** — this half imports `.client`): `../.venv/bin/python -m tiktoksearch.capture_diff captured_requests.jsonl`. Add `--identities <path>` (or `--config <profile>`) when the warm identity file is not where the server would find it — the resolution order mirrors `api/app.py` `_resolve_identities_path`: explicit `--identities` > `$TIKTOK_IDENTITIES_PATH` > the config's `identities_path` (relative to the config's directory) > `identities.json` beside the config. It reads that file's `device_query` **key names only** — never a `cookie`, an `x_tt_token`, or any value.
4. Add `--identity <device_id|index>` when the file holds **more than one** identity, naming the one whose params the capture should be diffed against (the index is the pool's `id0`, `id1`, … order). Without it the report claims only the `device_query` keys **every** entry shares. Both flags are stated intent: a path or selector that cannot produce the exact set **exits 3** and prints nothing rather than quietly answering with the fallback heuristic. Omit both to accept the heuristic deliberately.

Reading it:

- **The path listing comes first, and it is the most valuable output.** A busy path with no `*` next to it — no `*` means this client never calls it — is the finding that ends the hunt early: it says the app fetches the post grid somewhere other than where `client.py` assumes, and no param diff on our assumed path could have told you that.
- `[SUSPECT]` marks a missing param the app sends and **nothing** we send does. That is the shortlist to try in `_posts_params` / `_profile_params`.
- `[replayed]` marks a missing param that `client._common_params` really does send: a **key of the selected identity's `device_query`**, or one of the five it adds unconditionally (`device_id`/`iid`/`aid` + fresh `ts`/`_rticket`). Those five need no identity file to be true, so they keep this marker even in fallback mode; the `device_query` half is read straight from the file, keys only. The marker is a statement about our own request: genuinely not a gap.
- **The replay set is scoped to ONE identity, and that is the whole game.** `_common_params` does `dict(cfg.device_query)` for the single identity its client was built from, so unioning `device_query` keys across a heterogeneous file credits identity A with a key only identity B carries — a genuine suspect then prints `[replayed]` and the tool hides the answer it exists to find. `--identity` names the entry (**exact**); without it the report **intersects** across entries, because the pool chooses the slot and an intersection can only ever *under*-claim `[replayed]`. Under-claiming leaves a non-suspect visible in the shortlist, which costs a glance; over-claiming costs the hunt. The header line states which of the three sources is in use and with what scope.
- `[also-on-search]` is the **fallback** marker, used for the heuristic half of the set when no identity file is readable. It means only *the app also sends this on a search path we call* — **check it against the identity's `device_query` before dismissing it.** It is not evidence that we send it. The two sets diverge on exactly the class of param worth hunting: `device_query` is a pure device fingerprint, while a request-context param (`enter_from`, `pull_type`, `search_source`, `count`) rides on both the search request and the post-grid request without ever entering `device_query` — and a request-context param is the likeliest thing a bodyless-200 validator demands. Re-run with `--identities` to turn these into a real answer.
- `[under-filters]` marks a param the client does send, but only once a search filter is applied. `expected_params` builds the **unfiltered** video search, so a capture taken with a filter set would otherwise report `is_filter_search` / `filter_by` / `sort_type` / `general_filter_sort_type` / `publish_time` / `filter_selected` as suspects the client never sends. The names come from `filters.py` `to_query_params`, so they cannot drift.
- A trailing `(a search request sent '…' instead)` note means the app sent that same param name with a **different value** on a search path (`enter_from=others_homepage` on the post grid vs `search_result` on the search request). A value that changes with the request context is itself a clue about what the param means, so the report surfaces it rather than printing only the post-grid value.
- The per-path sections separate "**not observed in this capture**" from "*N* request(s) observed, but **not one carried a query param**". Those are opposite answers to the question being hunted, and one message for both reads as a failed capture session.
- `extra` is a param we send and the app does not — a candidate for being the thing the validator rejects.
- Values of device-identifying and credential-shaped params are masked `first6…last4` on the way *into* the file **and again on the way out to stdout** — the second pass matters because an unmasked value otherwise reaches terminal scrollback and any pasted report, not just a git-ignored file. `SENSITIVE_PARAM_NAMES` carries the alias row on purpose: the app sends the same identifier under two names in one query string (`install_id` = `iid`, `android_id` = `openudid`), and `google_aid`/`gaid`/`oaid`/`mac_address`/`imei` match no substring marker, so masking one spelling alone publishes the value under the other. **Names are never masked**, because a name is not a secret and names are the whole signal. Header **names** are recorded, values never. The output file still carries fingerprint material, so it is git-ignored — `captured_requests*.jsonl` plus `mobile/*.jsonl`, scoped rather than a repo-wide `*.jsonl` (which silently swallowed committed test fixtures), with a `!**/tests/**/*.jsonl` negation. Never commit a real capture.
- The capture file is **append-only**: it is a log, unlike `identities.json`, which is a state file the server hot-reloads and is therefore rewritten atomically only on a real credential change. Delete it between sessions if you want a clean diff.

`expected_params(kind)` — in `capture_diff.py`, the venv-only half — calls `client.py`'s own param builders (`_video_search_params`, `_user_search_params`, `_profile_params`, `_posts_params`), so the diff cannot drift from what the client really sends. **When a change to a param set is warranted, change the builder** — never the diff tool's copy, because there is none.

## Diagnose "empty / 100-100" results
1. `GET /health` — is any identity `usable`, or are they all `stale`? All stale → creds expired, refresh via capture loop.
2. Check logs for `empty result (attempt N, device=..., path=...)` — that's `hit_shark`, an identity problem, not a signer problem.
2b. **Read the reply BODY before concluding identity.** `hit_shark` is HTTP 200 with well-formed JSON, `status_code: 0`, and an empty item list. A *bodyless* 200, an nginx/TLB 404 page, a `"url doesn't match"` JSON, or a WAF challenge page are all something else entirely — wrong host, dead path, incomplete params, or a browser gate — and diagnosing any of them as expired credentials sends you to the capture loop for a problem it cannot fix. `architecture.md` § data flow invariants has all five shapes and how to tell them apart for free.
3. Confirm the identity has a `sessionid` cookie + `x_tt_token`; guest requests get empty results for many keywords.
4. Confirm a residential proxy is assigned — bare host IP is its own block vector.
