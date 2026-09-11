## Plan: RabbitMQ scraping worker   [EPIC]

### Context

The broker at `10.10.0.40:5672` is now reachable over the `wg0` WireGuard tunnel.
The inbound message format below was **read from the live broker** (management API,
`ackmode: reject_requeue_true`, nothing consumed) — it is measured, not assumed.

The job: consume scrape requests from two queues, serve them with the EXISTING
`/search` and `/profile` + `/user/posts` endpoints, and publish **one message per
post** back to the broker, each message carrying `search_type` so the consumer
knows where the record came from.

### Measured broker facts

Exchange `sm.scraping.tiktok`, type **direct**, durable. Routing key equals the
queue name for all four queues (no wildcards). Inbound messages arrive with
`delivery_mode: 2`.

Inbound `sm.scraping.tiktok.keyword`:

```json
{"keyword_id": 5038, "keyword_name": "Şəki", "is_auto_generated": false,
 "is_combined": false, "max_results": 30, "sort_type": null,
 "publish_time": null, "timestamp": "2026-09-09T11:52:33.589249"}
```

Inbound `sm.scraping.tiktok.page`:

```json
{"page_id": 1, "page_name": "sirabasc",
 "page_url": "https://www.tiktok.com/@sirabasc",
 "max_posts": 50, "timestamp": "2026-09-09T11:52:43.447566"}
```

Outbound: **all** results — keyword and page alike — go to
`sm.scraping.tiktok.keyword.result` (confirmed by the requester).
`sm.scraping.tiktok.re.post` is NOT ours; the worker never touches it.

### Architecture decision

The worker is a **separate process** that calls the running FastAPI service over
HTTP on `localhost`.

Rationale: the keyword union, the author filter, the newest-first ordering, the
cap accounting and the `page_token` logic all live INSIDE the `app.py` handlers,
not in reusable functions. A worker importing `ClientPool` directly would have to
reimplement all of it — two divergent behaviours and two test suites for one
contract. An HTTP call duplicates nothing and consumes the reviewed contract.
The cost is that the API server must be running; when it is not, the worker
requeues the message rather than dropping it.

### Dependency

`pika` — a new dependency, justified: AMQP 0-9-1 has no stdlib support and
hand-rolling the protocol is plainly worse. Added to `requirements.txt`.
No other new dependency: `.env` is parsed by a small stdlib helper rather than
adding `python-dotenv`.

### Contracts

#### Inbound → upstream call

`sm.scraping.tiktok.keyword`:

| inbound field | use |
|---|---|
| `keyword_name` | `POST /search` `query` |
| `max_results` | `POST /search` `limit` |
| `sort_type` | `filters.sort_type` when non-null |
| `publish_time` | `filters.publish_time` when non-null |
| `keyword_id`, `keyword_name` | echoed on every outbound message |
| `is_auto_generated`, `is_combined`, `timestamp` | ignored (not actionable here) |

`type` is always `keyword` for this queue.

`sm.scraping.tiktok.page`:

| inbound field | use |
|---|---|
| `page_url` | handle extracted from it; `page_name` is the fallback |
| `max_posts` | `POST /user/posts` `limit` |
| `page_id`, `page_name` | echoed on every outbound message |
| `timestamp` | ignored |

Two calls per page message: `POST /profile` for the profile metadata, then
`POST /user/posts` with `period` at the endpoint default (30).

#### Outbound message

Published to exchange `sm.scraping.tiktok`, routing key
`sm.scraping.tiktok.keyword.result`, `delivery_mode=2` (persistent),
`content_type: application/json`, UTF-8.

**The message IS the post.** There is no wrapper object: the flattened video
record's own fields sit at the TOP LEVEL of the message body. `search_type` and
`post_url` join them there, because both describe the post. Everything that is
NOT about the post — which job produced it, when we scraped it, and the account
profile — lives under a single `metadata` object. One post is one message; a
result list of N posts becomes N messages.

Every key is ALWAYS present, at both levels; inapplicable ones are `null`, so a
consumer never needs a key-existence check.

```json
{
  "id": "7680105451131882770",
  "description": "Bizi bu qədər çox sevdiyiniz üçün təşəkkür edirik 💚",
  "create_time": "2026-08-31T08:11:48+00:00",
  "author_username": "sirabasc",
  "author_unique_id": "sirabasc",
  "author_id": "7195575867517944837",
  "author_sec_uid": "MS4wLjABAAAAw4DV…",
  "region_code": "AZ",
  "view_count": 138432,
  "like_count": 916,
  "comment_count": 1,
  "share_count": 25,
  "hashtags": [],
  "music_id": "7680105512609090305",
  "music_title": "original sound - sirabasc",
  "duration": 26411,
  "post_url": "https://www.tiktok.com/@sirabasc/video/7680105451131882770",
  "search_type": "page",
  "metadata": {
    "keyword_id": null,
    "keyword_name": null,
    "page_id": 1,
    "page_name": "sirabasc",
    "scraped_at": "2026-09-10T08:40:39Z",
    "source_term": "search:sirabasc",
    "profile": {
      "username": "sirabasc",
      "profile_url": "https://www.tiktok.com/@sirabasc",
      "user_id": "7195575867517944837",
      "sec_uid": "MS4wLjABAAAAw4DV…",
      "display_name": "Sirab",
      "signature": null,
      "follower_count": 309729,
      "following_count": 0,
      "aweme_count": 372,
      "heart_count": 1001433,
      "region_code": null,
      "verified": false,
      "private": false,
      "avatar_url": "https://p19-common-sign.tiktokcdn.com/…",
      "source": "user_search"
    }
  }
}
```

This shape was reviewed by the requester against two live messages (one keyword
job, one page job) and approved. It is the contract.

**Top level** — the `flatten_video` record verbatim, EXCEPT that `source_term`
is moved out of it into `metadata` (it records which search surfaced the record,
which is a fact about our scrape, not about the post), plus two added fields:

- `search_type` — `"keyword"` or `"page"`.
- `post_url` — see below.

Nothing else about the record is altered, renamed or dropped: a consumer that
already reads `/search` results reads these messages with the same code, minus
`source_term`, plus the two fields above.

**`metadata`** — everything not about the post. Always present, always an object:

- `keyword_id` / `keyword_name` — filled for `"keyword"`, null for `"page"`.
- `page_id` / `page_name` — filled for `"page"`, null for `"keyword"`.
- `scraped_at` — the worker's UTC publish time, ISO-8601 with a `Z` suffix. Note
  it differs in spelling from the record's own `create_time` (`+00:00`, from
  `mapping._iso_utc`, whose fixed 25-char width the posts ordering leans on).
  Both are UTC. The divergence is known and was accepted by the requester.
- `source_term` — the keyword that surfaced this record, moved here from the
  record.
- `profile` — for `"page"`, the `/profile` response with `device` and `elapsed_s`
  REMOVED (internal diagnostics, not the requester's data) and `profile_url`
  ADDED. Null for `"keyword"`. Note `signature` and `region_code` inside it are
  structurally always null on the current source (`user_search`) — that is not
  "this account has no bio", and `ProfileResponse` documents why. `avatar_url` is
  a signed CDN URL carrying `x-expires`, so it goes stale; archiving the image is
  the consumer's call.

**The two URL fields are BUILT HERE, in the broker layer, and deliberately not
added to `mapping.py`.** Neither `flatten_video` nor `flatten_profile` carries a
URL today. `mapping.py` defines the `/search` HTTP response shape, so adding a
field there changes the contract for every existing API consumer — out of scope
for a worker Epic. The broker builds them from fields the records already carry:

- `post_url` — `https://www.tiktok.com/@{author_unique_id}/video/{post id}`, from
  the record's own `author_unique_id` and `id`. **`author_unique_id` and NOT
  `author_username`**: the latter falls back to the user-settable, non-unique
  `nickname` (see the comment in `mapping.flatten_video`), and a URL built from a
  nickname points at the wrong account or at nothing. When `author_unique_id` is
  null — the author has no handle upstream — `post_url` is `null`. It is NOT
  fabricated from any other field and no placeholder handle is substituted: an
  unverified URL that looks real is worse than a null. For `page` results this
  case cannot arise, because those records are filtered to the requested handle.
- `metadata.profile.profile_url` — `https://www.tiktok.com/@{profile username}`,
  built from the profile's OWN `username` (TikTok's spelling of the handle), not
  from the inbound `page_url` and not from `page_name`. Null when `username` is
  null.

Both are percent-encoded where the handle requires it, and the base
`https://www.tiktok.com` is a named constant, not an inline string.

#### Ack policy

| outcome | action |
|---|---|
| every post message published | `ack` |
| permanent error (404 unknown handle, 422 validation) | `ack` + WARNING log — requeueing would loop forever |
| transient error (502 `SoftError`/`TransportError`, 503 pool busy/gone/stale, API unreachable, publish failure) | `nack(requeue=True)` + short backoff sleep, so the process does not spin |
| **429 daily cap exhausted** (`PoolExhausted` with `PoolCode.CAP`) | `nack(requeue=True)` + a LONG backoff — the cap resets on a day boundary, so retrying in seconds is pure waste. Distinguished from 503 on the status code, never on the message prose |
| 429 `RateLimited` (TikTok rate-limited us) | same as 503: `nack(requeue=True)` + short backoff. Both are 429, so the worker MUST NOT branch on the status alone — it reads the `detail` prefix or, better, the API is asked to keep them distinguishable; if it cannot, treat 429 as the long-backoff case, since over-waiting is cheap and hammering an exhausted cap is not |
| unparseable message body | `ack` + WARNING log (a malformed body will never parse) |

Zero posts is a SUCCESS, not an error: nothing is published, the message is
acked, one INFO line is logged. "One message per post" means zero posts is zero
messages.

### Backend Tasks

**broker package** (`mobile/tiktoksearch/broker/`, new)
- `messages.py` — Pydantic models for both inbound shapes and the outbound
  envelope. Validated at the boundary per `.claude/rules/code-standards.md`;
  unknown inbound fields are ignored, not rejected (the producer may add fields).
- `handle.py` — `handle_from_page(page_url, page_name)`: extract the TikTok
  handle from a profile URL, falling back to `page_name`. Rejects a URL that is
  not a TikTok profile URL rather than guessing.
- `envelope.py` — build the outbound messages: fan one result list out to one
  envelope per post, attach the profile only for `page`, strip `device` and
  `elapsed_s`.
- `api_client.py` — HTTP calls to the local API (`/search`, `/profile`,
  `/user/posts`); classify a response into success / permanent / transient.
- `consumer.py` — pika wiring: connect, declare nothing (the topology already
  exists — the worker must NOT redeclare or it may fight the producer's
  settings), `basic_qos(prefetch_count=1)`, consume both queues, apply the ack
  policy, publish results.
- `env.py` — minimal stdlib `.env` reader (no new dependency).

**entry point**
- `mobile/worker.py` — CLI: `--api-url`, `--queues`, `--prefetch`, `--log-level`.
  Reads broker credentials from the environment / `.env` only.

**config / docs**
- `requirements.txt` — add `pika`.
- `.env.example` — `RABBITMQ_*` KEY NAMES ONLY, no values.
- `agent_docs/architecture.md` — a section on the broker worker: the two inbound
  shapes, the single outbound routing key, why HTTP-to-local-API rather than
  in-process, and the ack policy.
- `CHANGELOG.md` — one line.

### Subtasks

- [x] 1. Pure logic: `broker/messages.py` (both inbound shapes + outbound envelope model), `broker/handle.py` (`handle_from_page`), `broker/envelope.py` (fan a result list out to one envelope per post). No I/O, no pika, no HTTP.
- [x] 2. I/O edges: `broker/env.py` (stdlib `.env` reader), `broker/api_client.py` (local-API calls + success/permanent/transient classification), `broker/consumer.py` (pika wiring, `prefetch_count=1`, ack policy, publish), `mobile/worker.py` (CLI). `requirements.txt` + `.env.example`.
- [x] 3. Tests: `test_broker_messages.py`, `test_broker_envelope.py`, `test_broker_consumer.py` per the Test Scope section below, each with the mutation table `.claude/rules/learned-lessons.md` requires. `pika` and the HTTP boundary stubbed; no test touches the live broker or TikTok.
- [ ] 4. Docs: `agent_docs/architecture.md` broker section, `CHANGELOG.md` line.

### Test Scope

Unit tests only; `pika` and the HTTP boundary are stubbed. **No test touches the
live broker or TikTok** (`CLAUDE.md` golden rule 3).

- `test_broker_messages.py` — both inbound shapes parse; the live samples above
  are the fixtures; missing required fields are a validation error; unknown extra
  fields are tolerated; `handle_from_page` on a real URL, on a bare name, on a
  non-TikTok URL, and on a URL with a query string / trailing slash.
- `test_broker_envelope.py` — a 12-post result list yields 12 envelopes; every
  envelope carries `search_type`; `profile` present for `page` and null for
  `keyword`; `device`/`elapsed_s` absent from the embedded profile; ids echoed;
  every key present even when null; an empty result list yields zero envelopes;
  a 200-post result list yields exactly 200 envelopes with 200 distinct `post_url`s;
  the record's fields are at the TOP LEVEL (no `post` wrapper) and `metadata` holds
  the job ids, `scraped_at`, `source_term` and `profile` — nothing else;
  `source_term` is absent from the top level and present in `metadata`;
  a record key colliding with `search_type`/`post_url`/`metadata` is detected rather
  than silently overwritten;
  `post_url` is built from `author_unique_id` and is null when that is null;
  `post_url` is NOT built from `author_username` (a record whose `author_username`
  differs from its `author_unique_id` must not produce a nickname-based URL);
  `profile.profile_url` is built from the profile `username`, not from the inbound
  `page_url`; a handle needing percent-encoding round-trips correctly.
- `test_broker_consumer.py` — the three ack paths (success acks, permanent error
  acks, transient error nacks with requeue); the publish call uses the exchange,
  the `sm.scraping.tiktok.keyword.result` routing key and `delivery_mode=2`; the
  worker declares no queues or exchanges; an unparseable body is acked, not
  requeued.

Every test over classification or ordering logic ships with a mutation table per
`.claude/rules/learned-lessons.md`: mutate the production rule, confirm the test
fails, restore.

### Verification

```
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
```

Live smoke (manual, after review):

```
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000
.venv/bin/python mobile/worker.py --api-url http://127.0.0.1:8000 --queues page --max-messages 1
```

Then read back one message from `sm.scraping.tiktok.keyword.result` with
`ackmode: reject_requeue_true` and check the envelope against the contract above.

### Rollback / safety

The worker is purely additive — no existing endpoint, handler or config changes.
Rollback is stopping the process. Inbound messages are acked only after every
outbound message is published, so a crash mid-job requeues the whole job rather
than half-publishing it. `mobile/identities.json` is never read or written here.
The broker password lives only in `.env` (git-ignored); `.env.example` carries
key names alone.

### Out of scope

- Concurrency. `prefetch_count=1`, one message at a time. The backlog is 347 page
  + 94 keyword messages and each page message spends ~4 device-cap units, so the
  daily cap will bind long before throughput does. Scaling out (N worker
  processes, or a prefetch above 1) is a separate decision that needs the cap
  budget, and is NOT part of this Epic.
- `sm.scraping.tiktok.re.post` — not ours.
- A "job finished, zero posts" signal. Zero posts publishes nothing; if the
  requester turns out to need an explicit signal, that is a follow-up.
- Any change to how `/search`, `/profile` or `/user/posts` behave.


---

# Plan: device-driven post harvesting   [EPIC 2]

### Context

`/aweme/v1/aweme/post/` — the account's real post feed — was **captured and
verified working** on 2026-09-10 from a Waydroid container running the genuine
TikTok 46.9.1 with `libhoudini` ARM translation and the mitmproxy CA in the
system trust store. Evidence (secrets masked):
`scratchpad/aweme_post_capture.txt`.

What was measured, in order, each one ruling out a hypothesis:

| finding | consequence |
|---|---|
| host is `api32-core-alisg.tiktokv.com`, not the configured `search19-normal-alisg` | our 404 was a wrong host; the right host answers |
| right host + our signer → HTTP 200, **0 bytes**, `tt_orcas_res: 1` | host was necessary, not sufficient |
| captured identity + captured params + 9 captured headers + our signer → still 0 bytes | params, headers and identity are NOT the gap |
| HTTP/1.1 vs HTTP/2, `x-bd-kmsv` dropped, `sdk-version: 1` → still 0 bytes | transport and header shape are NOT the gap |
| the app's OWN request replayed verbatim with plain `curl`, signature 611 s old → **HTTP 200, 108 976 bytes, `status_code: 0`, `aweme_list: 7`, `has_more: 1`** | TLS fingerprinting is NOT the gate; the signature is the ONLY remaining difference, and it is not narrowly time-bound |
| RapidAPI signer on the same request → 0 bytes, `tt_orcas_res: 1` | BOTH our signers are rejected; this is not a local-signer bug |

Conclusion: the `api32-core` gateway validates `x-argus` in a way neither of our
signers satisfies, while the search gateway does not. Reproducing 46.9.1's argus
is open-ended research. **But the app itself signs acceptably, runs locally, and
its traffic is already decrypted** — so the app becomes the data source rather
than a reference.

This also **bypasses the daily device cap entirely**: we issue no signed request,
so `daily_request_cap_per_device` (500/day, one device) does not bind this path.

### Division of labour — deliberately partial

- **`page` jobs** → device path. 1:1 with the app by construction: full feed,
  chronological, `has_more`/`max_cursor` pagination, `is_top` present.
- **`keyword` jobs** → UNCHANGED, still the existing `/search` API path. Driving
  search inside the app needs UI text entry and is fragile; opening a profile by
  intent is not. The working path is not touched.

### Contracts

No change to the outbound message contract (EPIC 1 § Contracts → Outbound
message). Records still go through `mapping.flatten_video`, so a consumer sees
the same shape whichever source produced it. The one honest difference is that
`metadata.source_term` has no keyword to name on this path — the plan's subtasks
must decide what it carries and say so, rather than inventing a fake keyword.

### Non-negotiable constraint

**The running worker must keep working.** Every change here is additive: a new
module tree, a new mitmproxy addon, and a new `--source` flag whose DEFAULT
preserves today's behaviour. No existing endpoint, handler, envelope field or
test may change semantics. `mobile/identities.json` is never read or written.

### Subtasks

- [ ] 5. Record the endpoint findings above in `agent_docs/architecture.md` (§ the user-scoped endpoints, § hit_shark): the real host, the five ruled-out hypotheses, `tt_orcas_res: 1` as the gateway signature, and that both signers fail where the app succeeds. This is the durable answer to a question that has now been investigated three times.
- [ ] 6. Harvest spool: a mitmproxy addon that writes each `/aweme/v1/aweme/post/` response to a spool directory as JSON, keyed by the request's `user_id` param plus a timestamp, alongside `has_more`/`max_cursor`. Must not disturb the existing `-w` flow file. Stdlib-only at import time, like `capture_requests_addon.py`, for the reason recorded there.
- [ ] 7. Device driver: fire `snssdk1233://user/profile/<user_id>` via `waydroid app intent`, then wait for a spool entry for that `user_id` newer than the intent. Bounded wait, explicit timeout, no unbounded polling. Report "no response" as a transient failure so the job requeues.
- [ ] 8. Wire it into the worker behind `--source {search,device}` (default `search`), page jobs only. Device failures classify transient. Then run it continuously.
- [ ] 9. Scroll-driven pagination (`has_more` follow-up via input swipe) — SEPARATE, and only after 6-8 are stable in production. Explicitly out of scope until then.

### Verification

Unit: `cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q` — the
baseline to preserve is **1183 passed**.
Live: one page job end to end, then read the published message back off
`sm.scraping.tiktok.keyword.result` with `ackmode: reject_requeue_true` and check
it against the approved envelope shape.

### Rollback

Stop the device worker; the search worker is a separate process and unaffected.


---

# Plan: local signing for the real user endpoints   [EPIC 3]

### Context

A spike (2026-09-11, 11 live arms) proved our vendored signer CAN mint a
signature `api32-core-alisg.tiktokv.com` accepts for `/aweme/v1/aweme/post/`,
once the argus payload is corrected. Arms D and A ran three minutes apart on the
IDENTICAL identity, params and `ts`:

| arm | what | result |
|---|---|---|
| D (control) | the app's own headers, 20.4 h old | 200, 108 840 B, `status_code=0`, 7 posts |
| A (baseline) | our shipped signer | 200, **0 B**, `tt_orcas_res: 1` |
| B | corrected argus + captured `f24` | 200, 108 935 B, 7 posts |
| E | B + **our own** gorgon and ladon | 200, 108 933 B, 7 posts |
| J | fresh `ts`, 20.6 h-old seed | 200, 109 040 B, 7 posts |
| F / G | no `f24` / `f24` omitted | 200, **0 B** |
| I | Arm B, only `f3` changed | 200, **0 B** |

Identity and endpoint were held constant and proven live, so neither is
implicated. **This is not `hit_shark`.**

What that settles:

- The argus crypto chain is fully solved — a real app `x-argus` was reproduced
  **bit-exact**. Nothing cryptographic remains.
- `f34/f35/f36` are **not** server-validated (B ≡ C). The "needs native disasm"
  branch is closed — do not spend effort there.
- `x-gorgon` and `x-ladon` are **not** validated on this endpoint (Arm E). Our
  gorgon is provably not the app's construction and is accepted anyway. Leave it.
- The one remaining capture dependency is the **pair `(f24 dyn_seed, f3 rand)`**.
  `f24` alone fails (G), a freely chosen `f3` fails (I). The pair is reusable
  across requests, served ~6 of them, and stayed valid ≥ 20.6 h. Timestamps are
  free. `f24` comes from `POST /ms/get_seed` on `mssdk16-normal-alisg`; the
  response is encrypted (176 B blob), and `get_seed` itself is signed with an
  argus carrying no `f24` — the bootstrap needs no seed.

### The profile endpoint is a DIFFERENT problem — do not conflate them

Measured on the same capture:

```
GET api32-core-alisg.tiktokv.com/tiktok/user/profile/other/v1
    the APP'S OWN request, valid x-argus  ->  200, 0 bytes, tt_orcas_res: 1
```

The app is refused there **with a signature we know is good**, at the same
moment the posts endpoint served it 7 items. So profile is not a signing
problem and correcting argus will not fix it. Two separate defects:

1. **Our path constant is wrong.** `client.USER_PROFILE_PATH` is
   `/aweme/v1/user/profile/other/`; the app calls `/tiktok/user/profile/other/v1`.
2. **The app was logged out** — no `sessionid`, no `x-tt-token` (measured). That
   is the most likely reason the gateway refuses it, but it is an INFERENCE, not
   a measurement, and the plan must treat it as one.

Correction to record: a 0-byte 200 with `tt_orcas_res: 1` is **not** proof of a
bad signature — here it happens to a good one. It is a general "the gateway
refused" signal, and the cause must be established per endpoint.

### Contracts

**Host routing.** `_get_signed` sends every direct request to `cfg.search_host`,
which 404s for these paths. A path→host resolution is needed; `client.py`
already has `ENDPOINT_PATHS` to hang it on.

**Identity material.** `(f24, f3)` is the same class of thing as the cookie: it
belongs on `Identity`, reaches `ClientConfig` through `overrides()`, is stored in
`identities.json`, and is collected by the capture addon. Same security handling
as the cookie — never logged, never committed, masked `first6…last4` in
diagnostics.

**`f3` becomes a parameter.** `generate_protobuf` currently derives
`rand_value = random.randint(...)` internally and feeds it BOTH to `f3` and to
`dyn_encode(rand=...)`. Passing the captured rand keeps the two consistent by
construction. With no pair present, the existing random behaviour stays.

### Subtasks

- [x] 10. **GATE — PASSED 2026-09-11.** The pair is IDENTITY-scoped, not request-scoped: Arm K served a DIFFERENT `user_id` (139 347 B, 9 posts) and Arm L the client's reduced 43-param set (109 036 B, 7 posts), both on the held-constant pair, with Arm N confirming the pair still live (7 posts). One captured pair therefore serves many accounts. **Consequence for subtask 14:** `_posts_params` currently emits `sec_user_id`; the app's own working request omits it and the endpoint keys on `user_id` alone. An unvalidated `sec_user_id` (a 76-char feed-sourced sec_uid) produced a 0-byte `tt_orcas_res: 1` refusal in Arm M. Send `user_id` alone, or only a validated full-length sec_uid. ~~GATE — measure whether the pair transfers.~~ Does the same `(f24, f3)` serve a DIFFERENT `user_id` and the minimal param set `_posts_params` actually sends (5 params), rather than the 54-param set it was captured with? The spike varied only `ts`/`_rticket`. If the pair is bound to its capture's params or user, the rest of this Epic is worthless — **stop and hand back to `/spike`**. Measure before writing code.
- [ ] 11. `helpers/argus.py`: `generate_protobuf` field corrections — `f15` → `{1: 9<<1, 6: 3<<1, 7: ts<<1}` (`f15.7` is `ts`, not `app_launch_time`, which becomes unused); `f23.2` → `9<<1`, `f23.4` → `76593152<<1`, add `f23.5` → `13631488<<1`; `f25` → `3<<1`; `f28` → `5<<1`; add `f33` → `2<<1`; `f29` → `2<<1`, `f30` → `6<<1`, `f31` → `477937796<<1` (the current code emits `f29=516112` and `f30=6` unshifted — both wrong); stop emitting `f5` (device_id) and `f16`; add a `rand` parameter. `encode_argus_fn`: header byte 7 → `0x00`, outer AES pad → length-only random filler (the inner protobuf pad stays PKCS7).
- [ ] 12. `config.py` + `config_direct.yaml`: `sign_mssdk_ver_str` → `v05.03.01-ov-android`, `sign_mssdk_ver_code` → `84082976`, a `posts_host`, and `sign_app_version` matching the identity's app.
- [ ] 13. `identity_manager.py` + the capture addon: carry and collect the `(f24, f3)` pair as identity material.
- [ ] 14. `client.py`: path→host resolution; fix `USER_PROFILE_PATH` to `/tiktok/user/profile/other/v1`; classify the gateway refusal as its own diagnosis rather than letting it read as `hit_shark`.
**LIVE-VERIFIED 2026-09-11 (subtasks 11-14, shipped code, not spike tooling):**
`client.py` + `signing.py` + `argus.py` + config, with a captured pair, returned
`status_code=0`, `aweme_list=7`, `has_more=1`, `max_cursor=1785097925107` from
`api32-core-alisg.tiktokv.com/aweme/v1/aweme/post/` — 43 params, real posts.

**New constraint measured the same day — WHERE the pair is harvested from matters.**
Two runs, same code, same device, same 43 params, each pair internally consistent
(seed and rand taken from ONE request):

| pair harvested from | result |
|---|---|
| `/aweme/v1/aweme/post/` | `status_code=0`, 7 posts |
| `/service/2/app_log/` | 0 bytes, `tt_orcas_res: 1` |

Read as: the pair is bound to the endpoint it was minted for, so a capture must
make the app **actually visit a profile** — merely launching it is not enough.
Honest caveat: two variables differed (the seed itself and its source endpoint),
so this is not a clean isolation. Confirming it needs one capture session that
contains a posts request; the session available had none, because the app ANR'd
before reaching a profile. This also explains the earlier Arm I result.

**Contract reconciliation (spec review, 2026-09-11).** Two additions were made
that subtask 12 does not name. Both are functionally required and are hereby part
of the contract:

- `dyn_version` as a `ClientConfig` field with `DEFAULT_DYN_VERSION = 3`. It fills
  argus `f26.1` and the corrected payload cannot be built without it. Measured
  inert with no seed: the legacy payload is byte-identical whether it is set or
  unset.
- `LOCAL_APP_VERSION_MISMATCH_MSG` / `DEVICE_QUERY_APP_VERSION_KEY` — a WARNING
  when `sign_app_version` disagrees with the identity's own
  `device_query['version_name']`. It is a guard, NOT a substitute for subtask 12's
  fourth bullet: the value itself must still match, and it did not — the config
  carried `46.0.42` against a `46.9.1` identity, and the live verification hid it
  by overriding the field in the probe. Both are now corrected.

**Deferred for speed, at the user's explicit request (2026-09-11) — not dropped:**
subtask 15 (tests) and subtask 16 (pair TTL). Everything in subtasks 11-14 is
therefore unpinned: the two-condition payload gate, `_request_path`, path→host
routing, the three half-pair guards, `GatewayRefused` classification, the
`f5`/`f16` omission, and `rand` coming from the pair. The architect flagged two
of its blocking findings as ready-made test cases — a trailing-slash `posts_host`
must still select the corrected payload, and a config-level pair must not ride an
identity that carries none.

- [ ] 15. Tests with a mutation table. Invented `(f24, f3)` values only — never real material.
- [ ] 16. Measure the pair's TTL beyond 20.6 h. This sets the capture cadence.
- [ ] 17. `/user/posts`: serve from the real feed when the identity carries a pair, else the existing search path. The fallback preserves today's behaviour.
- [ ] 18. Profile: after a logged-in identity exists, measure whether `/tiktok/user/profile/other/v1` returns data. Only then wire it. Do NOT build on the logged-out inference.

### Test Scope

Unit tests stub at the signing boundary — no test reaches TikTok (`CLAUDE.md`
golden rule 3). A mutation table is mandatory (`.claude/rules/learned-lessons.md`
records nine mutations surviving a green 497-test run on this project):
every field constant must be breakable, plus the `f5`/`f16` omission, `rand`
coming from the pair rather than `random`, path→host routing, and the refusal
being distinguishable from `hit_shark`.

Baseline to preserve: **1345 passed**. Run tests FILE BY FILE under
`( ulimit -v 2097152; … )` — an unbounded loop in this repo took the machine
down twice on 2026-09-11, and the ulimit turns that into a failed test.

### Verification

```
cd mobile && ( ulimit -v 2097152; ../.venv/bin/python -m pytest tiktoksearch/tests/<file>.py -q )
.venv/bin/python mobile/api_signed.py --config mobile/config_direct.yaml --host 127.0.0.1 --port 8000
curl -s -XPOST localhost:8000/user/posts -H 'content-type: application/json' -d '{"username":"sirabasc","limit":30}'
```

### Out of scope

`/search` — it lives on another gateway where the existing signer WORKS; there
is no reason to touch it. `f34/f35/f36` derivation (not validated). Fixing
gorgon (not validated). Decrypting the `/ms/get_seed` response — that would
remove the capture dependency entirely, but it is a separate spike needing
native `libmetasec_ov.so` work, with the anti-frida/TTNet obstacles already on
record.

### Rollback / safety

Every change is additive: with no pair the signer keeps its current behaviour,
`/user/posts` falls back to the search path, and removing `posts_host` restores
the old host. `mobile/identities.json` is never read or printed; the capture
addon keeps updating it by atomic replace.
