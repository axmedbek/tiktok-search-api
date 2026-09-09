## Plan: device farm — adb device pool, per-device search tasks, control UI   [EPIC]

### Context
A second service inside this repo that drives real/virtual Android devices: it opens TikTok, runs a search, scrolls the results, and emits the records each scroll produces as JSON. Tasks are per-device (each device gets its own keyword/tag and parameters), and a task can be started, stopped, reconfigured and **refreshed** — refresh restarts the search from the top so newly-posted content is picked up. A separate UI lists `adb devices`, validates that each one is actually ready to search, and shows a success badge or an explicit list of what is missing.

Three findings from research shape this plan:

1. **The project already abandoned UI scraping.** `mobile/README_signed_api.md` states the signed path exists because it exposes "canonical `aweme_id`, `author_id`, view counts, music, hashtags, everything the UI-scraping path could not expose", and the old Appium README was deleted. So screen-reading is a dead end for record fidelity — hence the developer's choice of **network capture**.
2. **Network capture is a documented HARD BLOCKER.** ByteDance TTNet/Cronet ignores the system HTTP proxy and speaks QUIC over UDP 443, so a plain mitmproxy sees nothing; it needs a transparent proxy/VPN or a Frida Cronet hook, and the app ships anti-Frida (`.claude/skills/identity-capture/SKILL.md`, `agent_docs/common-changes-identity.md`). Whether it can be solved is **not** a plannable code change — it is a separate `/spike`.
3. **This machine has none of the tooling.** No `adb`, no emulator, no Android SDK, no mitmproxy, no frida, no connected device. The existing capture setup lives on a different (macOS) machine. The developer will install platform-tools here.

Consequence, and the reason this Epic is shaped the way it is: **everything except the record-extraction step is independent of the capture question.** Device discovery, setup validation, the pool, task lifecycle, the control API and the UI can all be built and reviewed now, behind a seam that the Spike later fills.

### Contracts

**A separate process, not a new router on the search API.** `POST /search` and `GET /health` keep their contracts untouched, and a misbehaving device layer must not be able to take the search API down. New entrypoint `mobile/api_devices.py` on its own port (default `8100`); the search API stays on `8000`.

New package `mobile/devicefarm/`:

| Module | Responsibility |
|---|---|
| `adb.py` | Thin wrapper over the `adb` binary. Enumerates devices (`adb devices -l`), runs shell commands, and reports the binary being absent as a first-class state rather than an exception. |
| `readiness.py` | Per-device setup validation → a list of named checks, each `pass` / `fail` / `unknown` with a human reason and a fix hint. |
| `tasks.py` | A task per device: search parameters, lifecycle state, and the scroll loop's bookkeeping (scroll count, records emitted, last error). |
| `pool.py` | The device registry: which devices exist, which are ready, which task each is running. Distinct from `tiktoksearch.pool` (that one is HTTP identities). |
| `source.py` | **The seam.** `ResultSource` protocol — "given a device running a search, yield the records this scroll produced". Ships with a stub implementation that returns nothing and reports `not_implemented`; the capture Spike replaces it. |
| `store.py` | Writes each scroll's records as JSON under `data/devicefarm/<device>/<task>/`. `data/` is already git-ignored. |
| `api/` | FastAPI app: device list + readiness, task CRUD, start/stop/refresh, task status. |

Control endpoints (all JSON, Pydantic-validated at the boundary per `.claude/rules/api-service.md`):

```
GET    /devices                     list + readiness per device
GET    /devices/{serial}/readiness  the full check list for one device
POST   /tasks                       create a task (device serial + search params)
GET    /tasks                       list tasks with state
PATCH  /tasks/{id}                  change search params (only while stopped)
POST   /tasks/{id}/start
POST   /tasks/{id}/stop
POST   /tasks/{id}/refresh          restart the search from the top
GET    /tasks/{id}/results          the JSON emitted so far (paged)
```

**Readiness checks** — derived from `.claude/skills/identity-capture/SKILL.md`; the developer should confirm this list, since it encodes what "setup complete" means for their capture loop:

| Check | How | Why it matters |
|---|---|---|
| `adb_binary` | `adb version` | Nothing works without it |
| `device_authorized` | `adb devices` state is `device`, not `unauthorized`/`offline` | The usual first failure |
| `tiktok_installed` | `pm list packages com.zhiliaoapp.musically` | The app under test |
| `tiktok_version` | `dumpsys package … versionName` == `46.0.42` | A version mismatch is a hit_shark cause (invariant (c)) |
| `logged_in` | best-effort; `unknown` when it cannot be determined without root | A cold app yields nothing |
| `root_available` | `adb shell su -c id` | Needed to place a CA in the system store |
| `mitm_ca_trusted` | CA present in the system **and APEX** trust store | Without it TLS interception fails |
| `frida_server` | `frida-server` process running | Needed for the Cronet hook |
| `traffic_redirect` | QUIC blocked / transparent redirect in place | The documented HARD BLOCKER |
| `screen_on` | `dumpsys power` | A locked device cannot be driven |

A device is **ready** only when every non-`unknown` check passes. The UI shows a success badge for ready devices, and for the rest the failing check names, each with its reason and fix hint.

**UI:** a new `mobile/devices.html`, standalone, same visual language as `demo.html` (same CSS tokens, Azerbaijani strings). A link is added in `demo.html` pointing at it, and a link back. No framework, no build step — a static file, consistent with how `demo.html` is served.

### Backend Tasks
- `mobile/devicefarm/adb.py` — device enumeration + shell exec; absent binary is a state, not a crash.
- `mobile/devicefarm/readiness.py` — the check table above; each check returns a named result with reason + fix hint. Pure functions over adb output so they are unit-testable without a device.
- `mobile/devicefarm/tasks.py`, `pool.py` — task lifecycle (`created → running → stopped`), one task per device, refresh = restart from the top with the same params, params editable only while stopped.
- `mobile/devicefarm/source.py` — the `ResultSource` seam plus a `StubSource` that reports `not_implemented`, so the whole control plane is exercisable before the Spike lands.
- `mobile/devicefarm/store.py` — per-scroll JSON under `data/devicefarm/…`, carrying the same envelope shape the keyword harvest already uses (`project`/`keyword`/`harvested_at`/`filters`/`results`).
- `mobile/devicefarm/api/` + `mobile/api_devices.py` — the FastAPI app and entrypoint on port 8100.
- `mobile/devices.html` — the control UI; `mobile/demo.html` — one link across.

### Subtasks
- [ ] `adb.py` + `readiness.py` (the half that is pure logic and fully testable without a device)
- [ ] `pool.py` + `tasks.py` — lifecycle, one task per device, refresh semantics
- [ ] `source.py` seam + `store.py` JSON output
- [ ] `api/` + `api_devices.py` — control endpoints
- [ ] `devices.html` + the `demo.html` link
- [ ] Verify: readiness against a real device (**BLOCKED — no adb here**)

### Test Scope
- **Unit (pytest, `/end`):** every readiness check against captured `adb` output fixtures (authorized/unauthorized/offline, app absent, wrong version, no root, CA missing); task lifecycle transitions incl. illegal ones (editing params while running, starting twice, refreshing a stopped task); pool assignment (one task per device, unknown serial); store envelope shape and path layout; `StubSource` reporting `not_implemented` rather than silently yielding nothing. The `adb` binary is stubbed at the `adb.py` boundary — **never shell out to a real device in a unit test**, mirroring the existing "never hit TikTok in a unit test" rule. The committed `HTTPAdapter.send` tripwire stays green.
- **integration-tests:** applies to the control plane wiring (pool ↔ task ↔ source ↔ store), all stubbed.
- **api-verifier:** applies to the new control endpoints; with no device present it verifies the empty-pool and not-ready paths, which is genuinely most of the surface.

### Verification
```bash
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q   # 293 baseline + new

.venv/bin/python mobile/api_devices.py --host 127.0.0.1 --port 8100
curl -s localhost:8100/devices | python3 -m json.tool     # [] with adb absent, and says so
```
**Verification blocker, stated plainly:** with no `adb` on this machine and no device attached, the readiness checks and the scroll loop cannot be proven against real hardware. Everything above the `adb.py` boundary is testable with fixtures; nothing below it is. After `sudo apt install android-tools-adb` (or the full SDK) and a device/emulator, `GET /devices` is the first real check.

### Out of scope
- **The capture mechanism itself.** `ResultSource` ships as a stub. Whether full JSON can be captured off a QUIC/anti-Frida device is a `/spike`, not part of this Epic — and if that Spike concludes "not feasible", this control plane still stands and the seam takes a different implementation (screen scraping at reduced fidelity, or driving the device only as a freshness trigger while the HTTP API supplies records).
- Installing adb/SDK/emulator, rooting, CA placement, Frida setup — operator work, and the readiness UI exists precisely to report on it.
- Any change to `POST /search`, `GET /health`, the signer, the identity pool, or `page_token` pagination.
- Scheduling/cron, multi-user auth, remote device farms over TCP (`adb connect`) — the pool is local-`adb` for now.

### Rollback / safety
Entirely additive: a new package, a new entrypoint, a new static page, plus one link line in `demo.html`. `git rm -r mobile/devicefarm mobile/api_devices.py mobile/devices.html && git checkout mobile/demo.html` restores the current state. The search API is a separate process and is not touched. `mobile/identities.json` is never read by this service. No new dependency: `adb` is invoked as a subprocess, and FastAPI/uvicorn/pydantic are already present.
