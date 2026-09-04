# Spike — Research / Reverse-Engineering Investigation

Answer an **open, uncertain question** about how TikTok's mobile API, risk-control, or signer actually behaves — when the answer can't be planned as code up front. A spike produces **verified knowledge + a recommendation**, never merged production code.

**Usage:** `/spike <the question>` — e.g. `/spike is the 'working' provider also shadow-blocked, or just tiktanic?`

**When to use a spike instead of `/start`:**

| Use `/spike` | Use `/start` |
|---|---|
| "Why did search start returning empty last week?" | "Add a `region` filter to search" |
| "Is TikTok on v47 now — is our signer stale?" | "Switch the default provider to `working`" |
| "Can we capture a fresh token without the emulator?" | "Add per-identity request logging" |
| "Does endpoint X still exist / did the response shape change?" | "Fix the cursor off-by-one" |
| The outcome is **knowledge**; you don't know what code (if any) changes | The outcome is a **known code change** |

If you already know what to build → `/start`. If you must first find out what's true → `/spike`.

---

## Flow

```
/spike <question>
  │
  ▼
Step 0: Sharpen (silent) — turn the ask into a falsifiable claim + ranked hypotheses
  │
  ▼
Step 1: Preflight — is this even a code question? (health/identity check first)
  │
  ▼
Step 2: Delegate → researcher agent (boxed investigation)
  │
  ▼
Step 3: Present findings + fork verdict + recommendation
  │
  ▼
Step 4: Route the outcome
  ├─ code change needed → "Run /start with: <the change>"
  ├─ identity/env issue → refresh identities (capture loop) — not a code bug
  ├─ provider/config switch → apply as a tiny change (or route to /start)
  └─ hypothesis refuted / no action → record finding, done
```

---

## Step 0 — Sharpen the question (silent)

Restate the user's ask as ONE falsifiable claim with a pass/fail. List the ranked hypotheses. If the ask is too vague to falsify, ask **one** sharpening question before spending any probe. A spike without a falsifiable claim is a rabbit hole.

## Step 1 — Preflight (the identity-first reflex)

Before treating anything as a code/signer question, rule out the #1 cause on this project — a dead identity:

```bash
curl -sIm 3 http://127.0.0.1:8000/health | head -1   # server up?
curl -s -m 5 http://127.0.0.1:8000/health | python3 -c "import sys,json; d=json.load(sys.stdin); print('identities:', d.get('identities'))"
```

- Server down → start it: `.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000`
- `usable: 0` (all identities stale) → the spike's answer to most "empty" questions is already **identity, not code**. Say so; the fix is the capture loop (`identity-capture` skill), not investigation. Only proceed into a code/signer spike once at least one identity is usable OR the question is explicitly about the signer/endpoint independent of identity.

## Step 2 — Delegate to the researcher agent

Launch the `researcher` agent via `Task()` with a **step box**:

```
Task() → researcher

Question: [the sharpened, falsifiable claim]
Hypotheses (ranked): [h1, h2, ...]
Box: [e.g. ≤ 8 probes / ≤ 1 RapidAPI sign / read-first]
Known context: [relevant files, prior findings, memory pointers]
```

The researcher reads the relevant skills (`reverse-engineering`, `tiktok-signing`, `anti-block-evasion`, `identity-capture`), designs the cheapest discriminating probe per hypothesis, runs it, and returns a findings report with a **signer-vs-identity-vs-endpoint fork verdict**.

**Constraints the researcher honors (restate if needed):** never loop-hammer TikTok, ≤ 1 RapidAPI sign per hypothesis (quota is money), mask all secrets, throwaway probes only (`/tmp` or `scratch-*.py`) — no edits to `mobile/tiktoksearch/` production code.

## Step 3 — Present findings

Show the researcher's report to the user verbatim-ish: the claim verdict (CONFIRMED / REFUTED / INCONCLUSIVE), the evidence, the **fork verdict** (signer / identity / endpoint-shape), and the recommendation.

## Step 4 — Route the outcome

| Outcome | Action |
|---|---|
| A code change is warranted | Do NOT implement here. Tell the user: **"Run `/start` with: `<the specific change from the recommendation>`"** so it goes through plan → review → test. |
| Identity/environment issue (stale creds, no proxy) | Not a code bug. Point to the `identity-capture` skill / capture loop; offer to help refresh `identities.json`. |
| Trivial config/provider switch | Offer to apply it directly as a one-liner, or route to `/start` if it touches logic. |
| Hypothesis refuted / no action | Record the finding. Nothing to build. |
| Memory-worthy durable fact | Offer to add it to `agent_docs/architecture.md` (§ hit_shark / § signer) or project memory. |

**A spike never edits production code.** It ends by handing a concrete, planned change to `/start` — or by concluding there's nothing to build.

---

## Rules

- Identity-first: check `/health` before treating "empty" as a code/signer bug.
- One falsifiable claim per spike. Sharpen before probing.
- The researcher is boxed — it reports even when inconclusive rather than spiraling.
- ≤ 1 RapidAPI sign per hypothesis; never loop-hammer TikTok; mask secrets.
- Output is knowledge + a recommendation. Building is `/dev`'s job, reached via `/start`.
- If a spike keeps recurring on the same question → the finding belongs in `agent_docs/` so it's not re-investigated.
