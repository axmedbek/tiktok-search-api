from __future__ import annotations
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .errors import BrokerConfigError
from .messages import KeywordMessage, Metadata, OutboundMessage, PageMessage, SearchType

# `scraped_at` per the outbound contract: ISO-8601, second precision, `Z`
# suffix. Deliberately NOT `mapping._iso_utc`'s `+00:00` rendering -- that one
# is a RECORD field whose fixed 25-character width the posts ordering leans
# on, while this is the broker contract's own spelling of the publish time.
SCRAPED_AT_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
# `/profile` reports which pooled device served the call and how long it took.
# Both are OUR diagnostics, not the requester's data, so neither is
# republished. Named so the strip is one list rather than two literals.
PROFILE_INTERNAL_KEYS = ('device', 'elapsed_s')

# The two URL fields are built HERE and deliberately NOT in `mapping.py`:
# neither `flatten_video` nor `flatten_profile` carries a URL today, and
# `mapping.py` defines the `/search` HTTP response shape, so a field added
# there changes the contract for every existing API consumer.
TIKTOK_BASE_URL = 'https://www.tiktok.com'
_VIDEO_PATH_SEGMENT = 'video'
# The IDENTITY field on a record, and the only one a URL may be built from.
# `author_username` falls back to the user-settable, non-unique `nickname`
# (see the comment in `mapping.flatten_video`), so a URL built from it points
# at the wrong account or at nothing.
_AUTHOR_HANDLE_KEY = 'author_unique_id'
_POST_ID_KEY = 'id'
_PROFILE_USERNAME_KEY = 'username'
_PROFILE_URL_KEY = 'profile_url'

# The message IS the post, so the record's fields share the body's top-level
# namespace with the three fields this layer owns. These are those three.
# MEASURED: `mapping.flatten_video` emits 17 keys and none of them is one of
# these, so there is no collision today -- but the record's key set is
# `mapping.py`'s to change, and a silent merge would let a future field there
# either overwrite ours or be overwritten by it, with no symptom but a wrong
# value in every message.
RESERVED_TOP_LEVEL_KEYS = ('post_url', 'search_type', 'metadata')
# `source_term` is the one record field that MOVES rather than passes through:
# it names the search that surfaced the record, which is a fact about our
# scrape, so it belongs in `metadata`.
_SOURCE_TERM_KEY = 'source_term'
_METADATA_KEY = 'metadata'
_POST_URL_KEY = 'post_url'
_SEARCH_TYPE_KEY = 'search_type'


def keyword_envelopes(message: KeywordMessage, posts: Sequence[Mapping[str, Any]], *, scraped_at: datetime | None = None) -> list[OutboundMessage]:
    """One envelope per post for a keyword job; no profile, no page ids.

    An empty `posts` yields zero envelopes -- "one message per post" means
    nothing is published for a job that surfaced nothing. That is a success,
    not an error."""
    stamp = _stamp(scraped_at)
    return [_envelope(post, SearchType.KEYWORD, keyword_id=message.keyword_id, keyword_name=message.keyword_name, page_id=None, page_name=None, profile=None, scraped_at=stamp) for post in posts]


def page_envelopes(message: PageMessage, posts: Sequence[Mapping[str, Any]], profile: Mapping[str, Any], *, scraped_at: datetime | None = None) -> list[OutboundMessage]:
    """One envelope per post for a page job; every one repeats the profile.

    The profile is attached to each message rather than sent once, because the
    contract is one self-describing message per post and a consumer reading a
    single message must not have to correlate it with another. Empty `posts`
    yields zero envelopes -- so a job whose profile was fetched but which
    surfaced no posts publishes nothing at all."""
    stamp = _stamp(scraped_at)
    public = public_profile(profile)
    return [_envelope(post, SearchType.PAGE, keyword_id=None, keyword_name=None, page_id=message.page_id, page_name=message.page_name, profile=dict(public), scraped_at=stamp) for post in posts]


def public_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """The `/profile` response as the broker republishes it.

    Two changes and no others: our internal diagnostics are removed, and
    `profile_url` is added. The added key is placed LAST rather than beside
    `username`; JSON object order is not part of the outbound contract, and
    every key the contract promises is present either way."""
    stripped = {key: value for key, value in profile.items() if key not in PROFILE_INTERNAL_KEYS}
    return {**stripped, _PROFILE_URL_KEY: profile_url(profile.get(_PROFILE_USERNAME_KEY))}


def post_url(post: Mapping[str, Any]) -> str | None:
    """`https://www.tiktok.com/@<handle>/video/<id>`, or None.

    Built from `author_unique_id` -- the record's IDENTITY field -- and NEVER
    from `author_username`, which falls back to the non-unique `nickname`.

    None when either part is missing: nothing is fabricated from another field
    and no placeholder handle is substituted, because an unverified URL that
    looks real is worse than a null. `author_unique_id` is null when the author
    has no handle upstream; for `page` results that cannot arise, since those
    records are filtered to the requested handle. A missing `id` cannot arise
    either -- `flatten_video` drops a record without one -- and is covered for
    the same reason: the alternative to a null here is a wrong URL."""
    handle = _url_part(post.get(_AUTHOR_HANDLE_KEY))
    post_id = _url_part(post.get(_POST_ID_KEY))
    if handle is None or post_id is None:
        return None
    return f'{TIKTOK_BASE_URL}/@{handle}/{_VIDEO_PATH_SEGMENT}/{post_id}'


def profile_url(username: Any) -> str | None:
    """`https://www.tiktok.com/@<username>`, or None when `username` is null.

    From the profile's OWN `username` (TikTok's spelling of the handle), not
    from the inbound `page_url` and not from `page_name`: those are what the
    producer believes the account is called, and this field states what TikTok
    answered."""
    handle = _url_part(username)
    if handle is None:
        return None
    return f'{TIKTOK_BASE_URL}/@{handle}'


def body(envelope: OutboundMessage) -> dict[str, Any]:
    """The wire body: the record's fields at the top level, ours beside them.

    Assembled in the contract's own key order -- record, then `post_url`,
    `search_type`, `metadata` -- so a message diffs cleanly against the
    approved sample. The record goes in FIRST and the three reserved keys
    after, but that ordering is presentation only: a collision has already
    been rejected by `_envelope`, so no assignment here can overwrite a record
    field.

    Nothing is dropped for being null. The contract promises a consumer that
    every key is present at both levels, so a null must serialise as `null`
    rather than vanish -- hence `model_dump` on `metadata` without
    `exclude_none`, and no filtering of `post_url`."""
    return {**envelope.record, _POST_URL_KEY: envelope.post_url, _SEARCH_TYPE_KEY: envelope.search_type.value, _METADATA_KEY: envelope.metadata.model_dump()}


def to_json(envelope: OutboundMessage) -> bytes:
    """The message body as UTF-8 JSON.

    `ensure_ascii=False`, so an Azerbaijani title stays readable on the wire
    instead of becoming `\\uXXXX` escapes -- the body is declared UTF-8.
    Encoding here rather than at the publish call keeps the body's shape next
    to the model that guarantees it."""
    return json.dumps(body(envelope), ensure_ascii=False).encode('utf-8')


def _envelope(post: Mapping[str, Any], search_type: SearchType, *, keyword_id: int | None, keyword_name: str | None, page_id: int | None, page_name: str | None, profile: dict[str, Any] | None, scraped_at: str) -> OutboundMessage:
    # ONE place where a record is split into "the post" and "our metadata", so
    # the collision check and the `source_term` move cannot be applied on one
    # queue and forgotten on the other.
    record = dict(post)
    collisions = [key for key in RESERVED_TOP_LEVEL_KEYS if key in record]
    if collisions:
        # RAISE, and deliberately not "log a warning and let ours win".
        #
        # These keys come from `mapping.flatten_video` -- our own code -- so a
        # collision is not a runtime condition TikTok can cause; it can only
        # be introduced by a change to `mapping.py` in this repo, i.e. a
        # development-time event that should fail the first time it is
        # exercised. Letting ours win would silently redefine a record field
        # for every consumer, and a WARNING in a worker log is precisely the
        # kind of thing nobody reads for months.
        #
        # `BrokerConfigError` and NOT `MalformedMessage`/`ValueError`: the ack
        # policy acks those, which would DROP a perfectly good job because of
        # a bug in our own mapping. This escapes the policy, stops the worker
        # with the inbound message still unacked, and so the broker requeues
        # the job untouched. Key names only -- no record values.
        raise BrokerConfigError(f"record field(s) collide with the reserved top-level key(s) {', '.join(collisions)}: `mapping.flatten_video` now emits a name this layer owns")
    metadata = Metadata(keyword_id=keyword_id, keyword_name=keyword_name, page_id=page_id, page_name=page_name, scraped_at=scraped_at, source_term=record.pop(_SOURCE_TERM_KEY, None), profile=profile)
    # `post_url` is read off the record AFTER the pop, which is harmless -- it
    # uses `author_unique_id` and `id`, never `source_term`.
    return OutboundMessage(record=record, post_url=post_url(record), search_type=search_type, metadata=metadata)


def _url_part(value: Any) -> str | None:
    # Percent-encoded with NOTHING left safe, so a handle or id carrying a
    # '/', a '?' or a space cannot extend or re-point the URL it lands in.
    # `quote` leaves the unreserved set (letters, digits, '-._~') alone, so an
    # ordinary TikTok handle -- letters, digits, '.' and '_' -- passes through
    # unchanged and a non-ASCII one round-trips through `unquote`. These values
    # come from upstream, not from our own validated boundary, so they are
    # encoded rather than trusted.
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return quote(text, safe='')


def _stamp(moment: datetime | None) -> str:
    # One stamp per job, shared by every envelope in the batch: the posts of
    # one job were published together, and a per-message clock read would only
    # record how long the loop took.
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        # A naive datetime would be read as LOCAL time by `astimezone` and
        # silently stamped with the wrong instant.
        raise ValueError('scraped_at must be timezone-aware')
    return moment.astimezone(timezone.utc).strftime(SCRAPED_AT_FORMAT)
