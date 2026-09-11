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
and the profile is the same `POST /profile` response. The published message
shape is byte-comparable with the search path's, field for field.

**`keyword` jobs never reach this module.** They stay on `POST /search`,
deliberately: driving search inside the app needs UI text entry and is fragile,
while opening a profile by intent is not. `consumer.BrokerConsumer` routes only
`PageMessage` through the injected page source.

### The handle -> user_id resolve

A deep link addresses an account by NUMERIC uid, and a page job carries a
handle. `POST /profile` already resolves that, and it costs ONE cap unit — so
it is used as the resolver rather than a second resolver being written here.
Its response is needed anyway: every page envelope carries the profile, so
there is nothing to publish without it.

### Failure classification

Every `DeviceError` becomes `ApiCallError(Failure.TRANSIENT)` at ONE place
below, so the consumer's existing ack table decides what happens and there is
no second policy (`.claude/rules/learned-lessons.md`). Transient means
`nack(requeue=True)` plus a short backoff: a timed-out visit is retried, never
acked as a job that found nothing.
"""
from __future__ import annotations
import logging
from typing import Any, Mapping, Sequence

from ..device.driver import DeviceDriver
from ..device.errors import DeviceError
from ..mapping import flatten_video
from .api_client import ApiCallError, ApiClient, Failure
from .envelope import page_envelopes
from .handle import handle_from_page
from .messages import OutboundMessage, PageMessage

logger = logging.getLogger('tiktoksearch.broker.device_source')

# `source_term` on a device-harvested record. See the module's `_source_term`
# for why this value and not a keyword.
DEVICE_SOURCE_PREFIX = 'device:'
# The field of a `/profile` response that carries the numeric uid a deep link
# needs. `ProfileResponse` declares it required, so an absent one is our own
# API answering off-contract.
PROFILE_USER_ID_KEY = 'user_id'
_RECORD_ID_KEY = 'id'
_CREATE_TIME_KEY = 'create_time'
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

    def __init__(self, api: ApiClient, driver: DeviceDriver) -> None:
        self._api = api
        self._driver = driver

    def __call__(self, job: PageMessage) -> list[OutboundMessage]:
        """The outbound messages for one page job. One per post, zero for none.

        Raises `MalformedMessage` (via `handle_from_page`) for a `page_url`
        that is not a TikTok profile URL — acked by the existing policy — and
        `ApiCallError` for everything else, classified for that same policy."""
        handle = handle_from_page(job.page_url, job.page_name)
        # The handle -> user_id resolve, and the profile every envelope
        # carries, in ONE call that costs one cap unit. Not a second resolver:
        # `POST /profile` is the reviewed one and it has to be called anyway.
        profile = self._api.profile(username=handle)
        user_id = _resolved_user_id(profile)
        try:
            feed = self._driver.fetch_posts(user_id)
        except DeviceError as exc:
            # THE one classification point. Every device failure — a timeout,
            # a dead container, an unreadable body — is transient, so the
            # consumer's existing ack table requeues the job instead of
            # acking it with nothing published.
            raise ApiCallError(Failure.TRANSIENT, f'device harvest failed: {exc}') from exc
        records = device_records(feed.aweme_list, user_id, limit=job.max_posts)
        logger.info('page_id=%s served from the device: %d aweme(s) -> %d record(s) (has_more=%s)',
                    job.page_id, len(feed.aweme_list), len(records), feed.has_more)
        return page_envelopes(job, records, profile)


def device_records(awemes: Sequence[Mapping[str, Any]], user_id: str, *, limit: int) -> list[dict]:
    """`awemes` as `flatten_video` records: deduped, newest-first, trimmed.

    Three rules, each mirroring what the search path already does to the same
    endpoint's output:

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
    for raw in awemes:
        if not isinstance(raw, Mapping):
            continue
        record = flatten_video(dict(raw), _source_term(user_id))
        key = record.get(_RECORD_ID_KEY) if record else None
        if not record or not key or key in seen:
            continue
        seen.add(key)
        records.append(record)
    return _newest_first(records)[:limit]


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


def _resolved_user_id(profile: Mapping[str, Any]) -> str:
    value = profile.get(PROFILE_USER_ID_KEY)
    if not isinstance(value, str) or not value.strip():
        # TRANSIENT, and classified the same way `api_client.results` treats a
        # response with no `results` list: this is our OWN API answering off
        # its declared contract, not the producer's fault, so the job survives
        # until that is corrected rather than being acked away.
        raise ApiCallError(Failure.TRANSIENT, f'/profile answered with no `{PROFILE_USER_ID_KEY}`')
    return value.strip()


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
