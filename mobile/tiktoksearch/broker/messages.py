from __future__ import annotations
import logging
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ..filters import PublishTime, SortType
from ..limits import MAX_POSTS_LIMIT, MAX_QUERY_CHARS

logger = logging.getLogger('tiktoksearch.broker.messages')

# The producer is a separate system that may add fields at any time, so every
# inbound model IGNORES what it does not declare rather than rejecting it: a
# new field upstream must not turn every message into an unparseable body (and
# an unparseable body is acked and dropped, never requeued). The fields the
# plan lists as ignored -- `is_auto_generated`, `is_combined`, `timestamp` --
# are therefore simply NOT declared: declaring a field this worker does not
# act on would advertise that it does.
_INBOUND_CONFIG = ConfigDict(extra='ignore')

# `keyword_name` reaches `POST /search` as `query`, so the ceiling that
# endpoint already enforces is the ceiling here -- imported from `..limits`,
# not restated, so a message this worker accepts is never one the API rejects.
# `page_name` is not sent upstream but IS republished on every outbound
# envelope, and an unbounded producer string that we re-emit is a channel; it
# shares the bound rather than inventing a second number.
MAX_NAME_CHARS = MAX_QUERY_CHARS
# `max_results` / `max_posts` become the `limit` of `POST /search` and
# `POST /user/posts`. `MAX_POSTS_LIMIT` is the posts ceiling and derives from
# the search ceiling in `..limits`, so one import bounds both queues.
MAX_INBOUND_LIMIT = MAX_POSTS_LIMIT
# `page_url` is parsed and then discarded (never echoed, never sent upstream).
# The bound is the conventional URL ceiling, so a runaway string cannot reach
# the parser at all.
MAX_PAGE_URL_CHARS = 2048


class SearchType(str, Enum):
    """Which queue a result came from, carried on every outbound message.

    Not `filters.SearchKind`: that enum answers "what kind of TikTok search"
    (keyword / hashtag / user) and has no `page` member, because a page job is
    a queue, not a search mode."""
    KEYWORD = 'keyword'
    PAGE = 'page'


def _strip_name(value: Any) -> Any:
    # `mode='before'`, so the declared `min_length`/`max_length` then judge the
    # value that will ACTUALLY be used -- exactly as
    # `api.schemas._HandleRequest._normalise_username` does for `username`.
    # Without it `min_length=1` counts spaces: `keyword_name = "  "` parsed,
    # became `POST /search` `query="  "`, and spent a signed request and a
    # daily-cap unit on nothing. The cap is this project's scarce resource, so
    # a blank name is rejected here instead. A non-string is handed back
    # untouched for pydantic to report as the type error it is.
    if not isinstance(value, str):
        return value
    return value.strip()


class KeywordMessage(BaseModel):
    """One job off `sm.scraping.tiktok.keyword`.

    Field names and types are the live broker sample, not a guess. Everything
    declared here is required except the two filters, which arrived null."""
    model_config = _INBOUND_CONFIG

    keyword_id: int = Field(description='Echoed on every outbound message.')
    keyword_name: str = Field(min_length=1, max_length=MAX_NAME_CHARS, description='`POST /search` `query`, and echoed on every outbound message. Surrounding whitespace is stripped before the length bound, so a whitespace-only name is a malformed message rather than a wasted cap unit.')
    max_results: int = Field(ge=1, le=MAX_INBOUND_LIMIT, description='`POST /search` `limit`. No default: the live sample always carries one, and inventing a fallback would silently scrape a different amount than was asked for.')
    sort_type: Optional[SortType] = Field(default=None, description='`filters.sort_type` when non-null.')
    publish_time: Optional[PublishTime] = Field(default=None, description='`filters.publish_time` when non-null.')

    _strip_keyword_name = field_validator('keyword_name', mode='before')(_strip_name)

    @field_validator('sort_type', 'publish_time', mode='before')
    @classmethod
    def _accept_numeric_filter(cls, value: Any, info: ValidationInfo) -> Any:
        # Both filters were NULL in the only measured sample, so the
        # producer's non-null wire type is UNMEASURED -- `1` and `"1"` are
        # equally plausible. `SortType`/`PublishTime` are str-valued enums and
        # pydantic does not coerce int -> str, so the int spelling would be
        # read as a malformed body and the whole job dropped. This changes a
        # value's spelling, never the value; a missing filter still means "no
        # filter" and is never defaulted to one.
        if isinstance(value, bool):
            # `bool` is an `int` subclass, and `True` is not a filter value --
            # hand it back for pydantic to report as the type error it is.
            return value
        if isinstance(value, int):
            # The ONE line that turns "unmeasured" into measured. This branch
            # firing in production is the evidence that the producer sends the
            # int spelling; its silence over a run with non-null filters is
            # the evidence it sends strings. It carries the field name and the
            # filter value only -- both non-secret, both from the enum's own
            # small numeric domain -- and never the message body.
            logger.info('inbound filter %s arrived as int %d, coerced to its string spelling', info.field_name, value)
            return str(value)
        return value


class PageMessage(BaseModel):
    """One job off `sm.scraping.tiktok.page`.

    `page_url` is optional even though the live sample carries one: it is the
    PREFERRED source of the handle and `page_name` is the fallback, so a
    message without it is still serviceable. A `page_url` that is present but
    is not a TikTok profile URL is rejected rather than fallen back on -- see
    `handle.handle_from_page`."""
    model_config = _INBOUND_CONFIG

    page_id: int = Field(description='Echoed on every outbound message.')
    page_name: str = Field(min_length=1, max_length=MAX_NAME_CHARS, description='Handle fallback when `page_url` is absent, and echoed on every outbound message. Whitespace-stripped before the length bound, like `KeywordMessage.keyword_name`.')
    page_url: Optional[str] = Field(default=None, max_length=MAX_PAGE_URL_CHARS, description='TikTok profile URL the handle is extracted from.')
    max_posts: int = Field(ge=1, le=MAX_INBOUND_LIMIT, description='`POST /user/posts` `limit`.')

    _strip_page_name = field_validator('page_name', mode='before')(_strip_name)


class Metadata(BaseModel):
    """Everything about the SCRAPE rather than about the post.

    The requester's own rule for what belongs here: a fact about which job
    produced the record, when we scraped it, or which account it came from --
    never a property of the video itself. `source_term` sits here for exactly
    that reason (it records which search surfaced the record) even though
    `mapping.flatten_video` emits it on the record.

    Not one field carries a Pydantic default, which is what makes "every key
    always present" STRUCTURAL rather than conventional: a defaulted field can
    be omitted at construction, and the guarantee would then rest on each call
    site remembering to pass it."""

    keyword_id: Optional[int] = Field(description='Echoed from a keyword job; null on a page result.')
    keyword_name: Optional[str] = Field(description='Echoed from a keyword job; null on a page result.')
    page_id: Optional[int] = Field(description='Echoed from a page job; null on a keyword result.')
    page_name: Optional[str] = Field(description='Echoed from a page job; null on a keyword result.')
    scraped_at: str = Field(description="The worker's UTC publish time, ISO-8601 with a `Z` suffix. Deliberately spelled differently from the record's own `create_time` (`+00:00`, from `mapping._iso_utc`, whose fixed 25-char width the posts ordering leans on). Both are UTC; the divergence is known and accepted.")
    source_term: Optional[str] = Field(description='The keyword that surfaced this record, moved here off the record. Null if a record carries none.')
    profile: Optional[dict[str, Any]] = Field(description='On a page result, the `/profile` response minus our internal diagnostics and plus `profile_url`; null on a keyword result.')


class OutboundMessage(BaseModel):
    """One post, published to `sm.scraping.tiktok.keyword.result`.

    **THE MESSAGE IS THE POST.** On the wire there is no wrapper object: the
    record's own fields sit at the TOP LEVEL of the body, with `post_url`,
    `search_type` and `metadata` beside them. `envelope.to_json` assembles
    that body; this model holds the parts SEPARATELY, which is deliberate and
    is what makes the collision check possible at all -- merged into one dict
    at construction, a record key named `post_url` would already have
    overwritten ours (or been overwritten by it) with nothing left to detect.
    See `envelope.RESERVED_TOP_LEVEL_KEYS`.

    "Every key always present" holds at BOTH levels, but by different means,
    and the difference is worth stating: inside `metadata` it is structural
    (see `Metadata`), and at the top level the three fields THIS layer owns are
    always emitted -- `post_url` included, null or not. The rest of the top
    level is `mapping.flatten_video`'s dict passed through verbatim, so those
    keys are guaranteed by that function and not by anything here. This layer
    deliberately neither adds a key a record lacks nor drops one it carries
    (except `source_term`, which moves into `metadata`): a consumer that
    already reads `/search` results reads these bodies with the same code."""

    record: dict[str, Any] = Field(description="One `mapping.flatten_video` record with `source_term` REMOVED (it is in `metadata`), otherwise verbatim as the API returned it. Its fields become the body's top level. A result list of N posts becomes N of these messages.")
    post_url: Optional[str] = Field(description="The post's TikTok URL, built HERE from the record's own `author_unique_id` and `id` -- see `envelope.post_url`. Null when the record carries no `author_unique_id`; never fabricated from another field. Present at the top level either way.")
    search_type: SearchType = Field(description='Which queue this result came from. A property of the post as far as a consumer is concerned, which is why it is top level and not in `metadata`.')
    metadata: Metadata = Field(description='Everything not about the post.')
