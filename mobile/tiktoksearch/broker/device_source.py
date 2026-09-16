"""Serve a `page` job from the device instead of `POST /user/posts`.

Selected by `worker.py --source device`, which is NOT the default. The default
is `--source search`, so the worker running in production keeps the behaviour
it has today and this module is never constructed.

### What is different, and what deliberately is not

Different: where the posts come from. `/aweme/v1/aweme/post/` — the account's
real feed, chronological, with `has_more`/`max_cursor` — reached by opening the
profile in the genuine TikTok app, because that gateway rejects both of our
signers (see `device/driver.py` for the measurements). It costs no device-cap
unit, since no signed request is issued for the posts.

NOT different: everything a consumer sees. Records go through
`mapping.flatten_video`, the envelope is built by the same `page_envelopes`,
and the profile dict carries exactly the keys `POST /profile` answers with
(`source: 'device'`). The published message shape is byte-comparable with the
search path's, field for field.

**`keyword` jobs never reach this module.** They stay on `POST /search`,
deliberately: driving search inside the app needs UI text entry and is fragile,
while opening a profile by intent is not. `consumer.BrokerConsumer` routes only
`PageMessage` through the injected page source.

### The handle -> user_id resolve

Done BY THE DEVICE: `DeviceDriver.visit_handle` opens the profile's web URL in
the app, which resolves the handle itself and calls the profile endpoint the
spool captures. Measured 2026-09-14: `POST /profile` is a user SEARCH, which
never surfaces auto-generated handles (`@user12569217` -> 502 forever) and
spends the same per-device search quota `hit_limit` throttles. The device
resolve has neither problem and costs no cap unit.

### Failure classification

`ProfileUnavailable` — TikTok answered that the account is not there — is
`Failure.PERMANENT`: the job is acked and dropped, as a `/profile` 404 was.
Every OTHER `DeviceError` becomes `ApiCallError(Failure.TRANSIENT)` at the same
place, so the consumer's existing ack table decides what happens and there is
no second policy (`.claude/rules/learned-lessons.md`). Transient means
`nack(requeue=True)` plus a short backoff: a timed-out visit is retried, never
acked as a job that found nothing.
"""
from __future__ import annotations
import logging
import time
from typing import Any, Mapping, Sequence

from ..device.driver import DeviceDriver, DeviceProfile
from ..device.errors import DeviceError, HarvestTimeout, ProfileUnavailable
from ..mapping import flatten_video, to_int
from .api_client import ApiCallError, ApiClient, Failure
from .envelope import page_envelopes
from .events import EventLog
from .handle import handle_from_page
from .messages import OutboundMessage, PageMessage

logger = logging.getLogger('tiktoksearch.broker.device_source')

# `source_term` on a device-harvested record. See the module's `_source_term`
# for why this value and not a keyword.
DEVICE_SOURCE_PREFIX = 'device:'
# `source` on the envelope profile dict: which upstream reply it was read from.
PROFILE_SOURCE_DEVICE = 'device'
# How the device resolved the handle, on the `page.resolved` event.
# Pseudo-status on a harvest timeout so the consumer can count attempts per job
# the way it counts 502s; not an HTTP status the API ever answers.
DEVICE_TIMEOUT_STATUS = 598
RESOLVED_VIA = 'device'
# `label` on this module's events; the consumer spells its page label
# `<queue> page_id=<n>`, this is the same suffix without the queue.
PAGE_LABEL_PREFIX = 'page_id='
_RECORD_ID_KEY = 'id'
_CREATE_TIME_KEY = 'create_time'
SECONDS_PER_DAY = 86_400
# A record whose `create_time` is unknown sorts under this key. The empty
# string, because it compares LESS than every ISO-8601 timestamp, so under
# `reverse=True` those records land LAST.
#
# This mirrors `api/app.py`'s `UNKNOWN_CREATE_TIME_KEY` / `_create_time_key` /
# `_newest_first`, and it is COPIED rather than imported because importing
# `api.app` executes `api/__init__.py`, which eagerly builds the FastAPI app —
# measured at 156 ms plus `fastapi` and `starlette` resident, in a worker
# process that serves no HTTP (the reason `limits.py` exists at all). The two
# are REQUIRED to agree, and a comment saying they agree is not a mechanism:
# `test_worker_device_source.py` pins this ordering against
# `api.app._newest_first` on the same records, so a change to either one that
# is not made to the other fails.
UNKNOWN_CREATE_TIME_KEY = ''


class DevicePageSource:
    """A `page` job served by the app in the Waydroid container.

    Shaped as a callable so `BrokerConsumer` takes it as one injected
    strategy — the same seam discipline as `connect=` and `session=`, and the
    reason the consumer's default path is untouched when it is absent."""

    def __init__(self, api: ApiClient, driver: DeviceDriver, *, max_age_days: int | None = None, events: EventLog | None = None) -> None:
        # `api` is kept as a constructor seam for `worker.py` and the tests
        # although this path no longer calls the local API: the device does
        # the handle resolve now.
        self._api = api
        self._driver = driver
        self._max_age_days = max_age_days
        # Optional operator event log; None = no events (the default).
        self._events = events

    def __call__(self, job: PageMessage) -> list[OutboundMessage]:
        """The outbound messages for one page job. One per post, zero for none.

        Raises `MalformedMessage` (via `handle_from_page`) for a `page_url`
        that is not a TikTok profile URL — acked by the existing policy — and
        `ApiCallError` for everything else, classified for that same policy."""
        label = f'{PAGE_LABEL_PREFIX}{job.page_id}'
        handle = handle_from_page(job.page_url, job.page_name)
        try:
            device_profile, feed = self._driver.visit_handle(handle, want=job.max_posts)
        except ProfileUnavailable as exc:
            # TikTok said the account is not there. PERMANENT: acked and
            # dropped, the way a `/profile` 404 was — a redelivery cannot make
            # a deleted user exist.
            raise ApiCallError(Failure.PERMANENT, f'no such TikTok user: {exc}') from exc
        except DeviceError as exc:
            # THE one classification point. Every other device failure — a
            # timeout, a dead container, an unreadable body — is transient, so
            # the consumer's existing ack table requeues the job instead of
            # acking it with nothing published.
            raise ApiCallError(Failure.TRANSIENT, f'device harvest failed: {exc}', status=DEVICE_TIMEOUT_STATUS if isinstance(exc, HarvestTimeout) else None) from exc
        user_id = device_profile.user_id
        profile = profile_dict(device_profile)
        self._emit('page.resolved', label=label, handle=handle, user_id=user_id, sec_uid=device_profile.sec_uid,
                   username=device_profile.username, via=RESOLVED_VIA)
        records = device_records(feed.aweme_list, user_id, limit=job.max_posts,
                                 max_age_days=self._max_age_days)
        logger.info('page_id=%s served from the device: %d aweme(s) -> %d record(s) (has_more=%s, max_age_days=%s)',
                    job.page_id, len(feed.aweme_list), len(records), feed.has_more, self._max_age_days)
        self._emit('page.device_feed', label=label, user_id=user_id, awemes=len(feed.aweme_list), has_more=feed.has_more, kept=len(records))
        return page_envelopes(job, records, profile)

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._events is not None:
            self._events.emit(kind, **fields)


def device_records(awemes: Sequence[Mapping[str, Any]], user_id: str, *, limit: int,
                   max_age_days: int | None = None, now: float | None = None) -> list[dict]:
    """`awemes` as `flatten_video` records: age-cut, deduped, newest-first, trimmed.

    The age cut comes first and only when `max_age_days` is set: an aweme whose
    raw integer `create_time` is older than `now - max_age_days` days is
    dropped, and so is one with a missing or non-integer `create_time` — it
    cannot be shown to be recent, and the front of a feed is the strongest
    claim a message makes. `now` is `time.time()` unless injected (tests).

    Three more rules, each mirroring what the search path already does to the
    same endpoint's output:

    * **Dedup on `id`**, as `client._paginate_posts`' `SeenWindow` does — one
      response should not repeat an aweme, and if it does, one post must not
      become two messages.
    * **Newest-first**, as `api/app.py` orders `/user/posts` before publishing.
      Not the app's own order: the app leads its grid with PINNED posts
      (`is_top`), so trimming the app order to `limit` could drop this week's
      posts in favour of a pinned one from two years ago. The trim comes AFTER
      the sort, so what `limit` drops is always the least recent.
    * **Trim to `limit`**, the job's own `max_posts`. A job asked for at most
      that many posts and must not be answered with more.

    No author filter, and that is not an omission: `/aweme/v1/aweme/post/` is
    addressed BY `user_id`, so every aweme in the response is that account's by
    construction. `api/app.py._authored_by` exists because the search path
    reaches posts through a keyword search that returns other authors."""
    records: list[dict] = []
    seen: set[str] = set()
    cutoff = _age_cutoff(max_age_days, now)
    for raw in awemes:
        if not isinstance(raw, Mapping):
            continue
        if cutoff is not None and not _is_recent(raw, cutoff):
            continue
        record = flatten_video(dict(raw), _source_term(user_id))
        key = record.get(_RECORD_ID_KEY) if record else None
        if not record or not key or key in seen:
            continue
        seen.add(key)
        records.append(record)
    return _newest_first(records)[:limit]


def _age_cutoff(max_age_days: int | None, now: float | None) -> float | None:
    """The oldest acceptable `create_time` (epoch seconds), or None for no cut."""
    if max_age_days is None:
        return None
    return (time.time() if now is None else now) - max_age_days * SECONDS_PER_DAY


def _is_recent(raw: Mapping[str, Any], cutoff: float) -> bool:
    # A missing or non-integer `create_time` is NOT recent: the cut exists to
    # keep old posts out of a feed, and an undated one cannot prove otherwise.
    create_time = to_int(raw.get(_CREATE_TIME_KEY))
    return create_time is not None and create_time >= cutoff


def _source_term(user_id: str) -> str:
    """`metadata.source_term` for a device-harvested record.

    **There is no keyword on this path, and none is invented.** The value is
    `device:<user_id>`, and it is not a free choice — it is the convention
    `client.py` already established for this exact endpoint reached the other
    way: `POSTS_SOURCE_PREFIX` makes a signed `/aweme/v1/aweme/post/` record
    carry `posts:<user_id>`, with the comment "posts are reached by id, so
    there is no search term to carry — the owner's public user_id is the honest
    answer". The prefix names the SOURCE, so `device:` says which of the two
    ways this record was fetched and can never be read as a producer's search
    term.

    Null was the alternative and was rejected: `search_type` is `page` on both
    paths, so a null here would leave a consumer with no way to tell a
    device-harvested message from a searched one, and `source_term` is exactly
    the field that records "which stream produced this record"."""
    return f'{DEVICE_SOURCE_PREFIX}{user_id}'


def profile_dict(profile: DeviceProfile) -> dict[str, Any]:
    """The envelope `profile`: exactly the keys `POST /profile` answers with.

    `envelope.public_profile` strips nothing from it (no `device`/`elapsed_s`
    diagnostics here) and adds `profile_url`. `display_name` is the nickname."""
    return {'username': profile.username, 'user_id': profile.user_id, 'sec_uid': profile.sec_uid,
            'display_name': profile.nickname, 'signature': profile.signature,
            'follower_count': profile.follower_count, 'following_count': profile.following_count,
            'aweme_count': profile.aweme_count, 'heart_count': profile.heart_count,
            'region_code': profile.region_code, 'verified': profile.verified, 'private': profile.private,
            'avatar_url': profile.avatar_url, 'source': PROFILE_SOURCE_DEVICE}


def _create_time_key(record: Mapping[str, Any]) -> str:
    # `api/app.py._create_time_key`'s rule. A plain STRING key is correct
    # because `mapping._iso_utc` renders every timestamp as the fixed
    # 25-character `YYYY-MM-DDTHH:MM:SS+00:00`, so lexicographic order IS
    # chronological order. Anything that is not a non-empty string gets the
    # sentinel, so no comparison between a string and a non-string is possible.
    value = record.get(_CREATE_TIME_KEY)
    return value if isinstance(value, str) and value else UNKNOWN_CREATE_TIME_KEY


def _newest_first(records: list[dict]) -> list[dict]:
    # Undated records go to the END: the front of a newest-first list is the
    # strongest claim a message makes, and putting an undated record there
    # would assert it is the most recent post. `sorted` is stable and stays so
    # under `reverse=True`, so records sharing a timestamp keep the order the
    # device gave them.
    return sorted(records, key=_create_time_key, reverse=True)
