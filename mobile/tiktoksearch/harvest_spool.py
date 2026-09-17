"""The harvest SPOOL: what the mitmproxy addon writes, and what the driver reads.

`/aweme/v1/aweme/post/` — the account's real post feed — is served to the
GENUINE TikTok app running in the local Waydroid container and to nothing else
we can build. Measured 2026-09-10: the `api32-core-alisg` gateway answers our
own signatures (local signer AND the paid RapidAPI signer) with HTTP 200, a
0-byte body and `tt_orcas_res: 1`, while the app's own signature works — even
replayed from plain `curl` 611 s later. So the app is the DATA SOURCE, not a
reference, and mitmproxy — which already decrypts its traffic — is the tap.

This module is that tap's file format: the addon writes one JSON entry per
response, the device driver reads the newest entry for a `user_id`. Both sides
share this file so the schema, the atomic write and the eligibility rule cannot
drift apart, exactly as `capture_record.py` is shared by its addon and its CLI.

**DELIBERATELY STDLIB-ONLY, and that is why it sits at the top of the package
directory rather than inside `device/`.** The addon runs under `mitmdump`, on
the distro/pipx interpreter and NOT the project `.venv`, so it can import no
project dependency at all. It reaches this file by putting the PACKAGE
DIRECTORY on `sys.path` and importing `harvest_spool` as a top-level module,
because `tiktoksearch/__init__.py` eagerly imports `.client` -> `.signing` ->
`tiktok_signer` -> `gmssl`. That chain is not hypothetical: it once made
`capture_requests_addon.py` fail to load with `No module named 'gmssl'` while
the proxy came up healthy, and a whole capture session recorded nothing. The
test of the rule, on a bare system interpreter:

    PYTHONPATH=mobile/tiktoksearch python3 -c "import harvest_spool"

`device/driver.py` imports it the ordinary relative way, from the venv, where
the package import costs nothing.

### Entry schema

One JSON object per file, named `<key>-<captured_at in ns>.json` where `key` is
the percent-encoded account uid: the request's `user_id` param when it carries
one, else the account uid read off the body (see `account_uid`) — measured
2026-09-14 on 46.9.3, the app's own request carries `sec_user_id` and NO
`user_id`, so the body is where the key comes from in practice:

    {"user_id": "7195575867517944837", "captured_at": 1789012345.678901234,
     "status": "ok", "reason": null, "has_more": true,
     "max_cursor": 1756628308000, "status_code": 0,
     "path": "/aweme/v1/aweme/post/", "byte_length": 108976,
     "aweme_list": [ ... ]}

`has_more` and `max_cursor` are recorded ALONGSIDE the body so a consumer can
paginate later without re-reading the flow file — that is the plan's
requirement and the seam subtask 9 (scroll-driven pagination) will use.

### Three statuses, and why an empty feed is not one of them

`status: "ok"` means the body parsed to a JSON object carrying an `aweme_list`
LIST. That list may be EMPTY — an account with no posts, or the end of a feed —
and that is a legitimate result the driver returns as a result.

`status: "unreadable"` means a response arrived that we could not read: a
0-byte body (the `tt_orcas_res: 1` shape), a body mitmproxy could not decode, a
body that is not JSON, JSON that is not an object, or an object with no
`aweme_list` list. That last case is the one worth naming: an object without
the key is NOT "zero posts" (`.claude/rules/anti-block.md`), it is a reply we
did not understand, and the driver must be able to tell it apart from "no
response yet". A `reason` string — one of this module's own literals, never any
of the body — says which.

The raw body is never stored for an unreadable entry: only its `byte_length`.
A body we cannot parse can be binary, enormous, or both.

### Atomic writes

Every entry is written to a temp file in the spool directory and moved into
place with `os.replace`, for the reason recorded in
`.claude/rules/learned-lessons.md` about `identities.json`: a half-written file
must never be readable. The temp name carries a `.tmp` suffix so it cannot
match the reader's `*.json` filter even mid-write.

Retention is deliberately NOT implemented. The spool is evidence, entries are
~100 KB, and nothing here deletes a file the operator may want to inspect;
`FileSpool` reads only the entries newer than the visit that asked, so the
directory growing does not slow a visit down.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Collection, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlsplit

logger = logging.getLogger('tiktoksearch.harvest_spool')

# The one path this spool is about. The account's own post feed, on the
# `api32-core-alisg.tiktokv.com` gateway — NOT the `search19-normal` host the
# signed client uses, and not `/aweme/v1/user/profile/other/`.
POST_FEED_PATH = '/aweme/v1/aweme/post/'
# The request param the entry is keyed by WHEN PRESENT: the profile owner's
# public uid, the same value `POST /profile` answers with, and how the driver
# finds "the responses for this user". The 46.9.3 app sends `sec_user_id`
# instead, so the key falls back to the body (`account_uid`).
USER_ID_PARAM = 'user_id'
# The profile endpoint the app calls when a profile is opened by its WEB URL
# (`https://www.tiktok.com/@<handle>`), measured 2026-09-14 on 46.9.3. Its
# response is what resolves a handle to a uid without a user search.
PROFILE_PATH = '/tiktok/user/profile/other/v1'
# A web-profile intent resolves the handle here before fetching the profile.
# An invalid handle stops at this response: no PROFILE_PATH request follows.
UNIQUE_ID_PATH = '/aweme/v1/user/uniqueid/'
UNIQUE_ID_PARAM = 'id'
# The profile request's own account param (no `user_id` on 46.9.3, measured).
SEC_USER_ID_PARAM = 'sec_user_id'
# Profile entries live in a SUB-directory of the spool, so the posts reader's
# `os.listdir` never sees them and the two key spaces (uid vs handle) cannot
# collide in one directory.
PROFILES_DIRNAME = 'profiles'
# Where the body names the account: every aweme carries `author.uid` (the
# account's numeric uid, as a string) and a top-level `author_user_id`.
FIELD_AUTHOR = 'author'
AUTHOR_UID_KEY = 'uid'
AWEME_AUTHOR_USER_ID_KEY = 'author_user_id'

DEFAULT_SPOOL_DIRNAME = 'harvest_spool'
ENTRY_SUFFIX = '.json'
# `.tmp`, so a temp file can never match the reader's `*.json` filter, and a
# leading dot so it is not mistaken for an entry by a human either.
TEMP_PREFIX = '.harvest-'
TEMP_SUFFIX = '.tmp'
# Separates the key from the nanosecond stamp in a file name. A separator and
# not a bare concatenation: without it, key `123` would match a file belonging
# to key `1234`.
NAME_SEP = '-'
# A file name is bounded on every filesystem, and `user_id` arrives off a
# request query string. 64 chars is far more than a 19-digit uid needs.
MAX_KEY_CHARS = 64
# The characters `urllib.parse.quote` leaves alone that a key must NOT keep:
# `.` so no `..` or dot-file can be built, `-` because it is `NAME_SEP`, `~`
# for symmetry with the other two unreserved characters.
KEY_EXTRA_ENCODED = ('.', '-', '~')
NANOS_PER_SECOND = 1_000_000_000
# Two responses for one user cannot share a nanosecond from one single-threaded
# addon, but if a name is somehow taken the stamp is nudged rather than the
# existing entry overwritten — losing a captured response silently is the one
# outcome this file exists to prevent.
MAX_NAME_ATTEMPTS = 1000

STATUS_OK = 'ok'
STATUS_UNREADABLE = 'unreadable'
ENTRY_STATUSES = (STATUS_OK, STATUS_UNREADABLE)

FIELD_USER_ID = 'user_id'
FIELD_CAPTURED_AT = 'captured_at'
FIELD_STATUS = 'status'
FIELD_REASON = 'reason'
FIELD_HAS_MORE = 'has_more'
FIELD_MAX_CURSOR = 'max_cursor'
FIELD_STATUS_CODE = 'status_code'
FIELD_PATH = 'path'
FIELD_BYTE_LENGTH = 'byte_length'
FIELD_AWEME_LIST = 'aweme_list'
FIELD_STATUS_MSG = 'status_msg'
# The server's own wording for "this account has no posts" (measured).
NO_MORE_VIDEOS_MSG = 'No more videos'

STATUS_ERROR = 'error'
STATUS_UNAVAILABLE = 'unavailable'
# A 0-byte resolver reply (`tt_orcas_res: 1`, measured 2026-09-17 once the account hit TikTok's
# daily search cap): risk control refused the device, nothing about the handle is known.
STATUS_THROTTLED = 'throttled'
PROFILE_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_UNAVAILABLE, STATUS_THROTTLED)
# Measured 2026-09-16: the resolver answered 8196, "Unique ID is invalid",
# for a profile intent that otherwise timed out and blocked the page queue.
INVALID_UNIQUE_ID_CODE = 8196
FIELD_HANDLE = 'handle'
# Where the profile response keeps each value. MEASURED 2026-09-14 over 241
# captured `/tiktok/user/profile/other/v1` responses (46.9.3): identity and the
# post count sit under `common.*`; follower/following/like counts and region sit
# in a `biz_data` object nested at varying depth under `header.components`;
# NO `signature` / bio text key exists anywhere in the reply.
COMMON_KEY = 'common'
PROFILE_INFO_KEY = 'user_profile_info'
STATICS_INFO_KEY = 'user_statics_info'
ACCOUNT_INFO_KEY = 'user_account_info'
HEADER_KEY = 'header'
BIZ_DATA_KEY = 'biz_data'
UID_KEY = 'uid'
SEC_UID_KEY = 'sec_uid'
USERNAME_KEY = 'username'
NICKNAME_KEY = 'nickname'
AVATAR_KEY = 'avatar'
AVATAR_VARIANTS = ('avatar_medium', 'avatar_larger', 'avatar_thumb', 'avatar_300x300')
URL_LIST_KEY = 'url_list'
AWEME_COUNT_KEY = 'aweme_count'
HAS_CERT_KEY = 'has_cert'
PRIVATE_ACCOUNT_KEY = 'is_private_account'
FOLLOWER_COUNT_KEY = 'follower_count'
FOLLOWING_COUNT_KEY = 'following_count'
LIKE_COUNT_KEY = 'like_count'
REGION_KEY = 'region'
HEADER_COUNTER_KEYS = (FOLLOWER_COUNT_KEY, FOLLOWING_COUNT_KEY, LIKE_COUNT_KEY, REGION_KEY)
# Bounded so a hostile or malformed body cannot recurse without limit.
MAX_HEADER_DEPTH = 12

# Why an entry is unreadable. OUR OWN literals — no part of the body, and no
# exception text, ever reaches a `reason`: it is written to a file and logged.
REASON_UNDECODABLE = 'response body could not be decoded'
REASON_EMPTY = 'response body is empty (0 bytes)'
REASON_NOT_JSON = 'response body is not JSON'
REASON_NOT_OBJECT = 'response body is JSON but not an object'
REASON_NO_AWEME_LIST = 'response body carries no `aweme_list` list'


def default_spool_dir() -> str:
    """The default spool directory: `mobile/harvest_spool`.

    Resolved from THIS FILE and never from the process cwd, which is the lesson
    `.claude/rules/learned-lessons.md` records about a relative
    `identities_path`: the server resolved it against the launch directory,
    silently found no identities and fell back to synthetic devices. Here the
    two processes that must agree — `mitmdump` in `mobile/` and the worker
    wherever it is launched — would otherwise agree only by accident."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(package_dir), DEFAULT_SPOOL_DIRNAME)


def user_id_from_url(url: str) -> str:
    """The request's `user_id` param, or `''` when it carries none.

    First value wins on a repeated name — the convention `capture_record.py`
    already uses. An empty string is returned rather than None so the addon's
    guard is one falsiness check and no entry is ever keyed by `'None'`."""
    return _param_from_url(url, USER_ID_PARAM)


def sec_user_id_from_url(url: str) -> str:
    """The request's `sec_user_id` param, or `''` (same rule as `user_id_from_url`)."""
    return _param_from_url(url, SEC_USER_ID_PARAM)


def _param_from_url(url: str, param: str) -> str:
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name == param:
            return value.strip()
    return ''


def account_uid(aweme_list: Iterable[Mapping[str, Any]]) -> str:
    """The account's numeric uid as the body states it, or `''`.

    The first non-empty `author.uid` across `aweme_list`, else the first
    non-empty `author_user_id`, coerced to a DECIMAL STRING — a uid that is
    not an integer is not a uid, and the driver's `USER_ID_PATTERN` would
    refuse it anyway. `''` for an empty list or a list naming no account, so
    the caller's guard stays one falsiness check, as with `user_id_from_url`."""
    for key_path in ((FIELD_AUTHOR, AUTHOR_UID_KEY), (AWEME_AUTHOR_USER_ID_KEY,)):
        for aweme in aweme_list:
            value: Any = aweme
            for key in key_path:
                value = value.get(key) if isinstance(value, Mapping) else None
            number = _as_int(value)
            if number is not None:
                return str(number)
    return ''


def file_key(user_id: str) -> str:
    """`user_id` as the file-name segment an entry is keyed by.

    Percent-encoded down to `[A-Za-z0-9_%]` and nothing else, so the resulting
    key can carry no path separator, no `..`, no leading dot and no NUL — it
    cannot escape the spool directory, shadow another user's entries, or become
    a dot-file. `user_id` reaches here off a request query string (the app's
    own, but external data all the same) and this is the boundary that turns it
    into a file name. A real uid is decimal digits, so it passes through
    unchanged.

    `quote` alone is not enough: it leaves the unreserved `-._~` untouched, and
    two of those matter here. `.` would let `..` survive into a name, and `-`
    is `NAME_SEP` — encoding it is what makes `<key>-<stamp>.json` an
    unambiguous grammar rather than one that happens to parse today.

    `''` when there is nothing usable to key on; the caller must not write."""
    encoded = quote(user_id.strip(), safe='')
    for char in KEY_EXTRA_ENCODED:
        # Safe after `quote`: every escape it produced is `%` plus two hex
        # digits, none of which is one of these characters.
        encoded = encoded.replace(char, f'%{ord(char):02X}')
    return encoded[:MAX_KEY_CHARS]


def entry_name(user_id: str, captured_at: float) -> str:
    """The file name for one entry: `<key>-<captured_at in ns>.json`.

    The stamp is in the NAME as well as in the body, but only as a cheap
    prefilter for the reader — the body's `captured_at` is the authoritative
    value, because a name can be renamed and a body cannot be renamed into
    agreeing with itself."""
    return f'{file_key(user_id)}{NAME_SEP}{int(captured_at * NANOS_PER_SECOND)}{ENTRY_SUFFIX}'


def nanos_from_name(name: str) -> int | None:
    """The nanosecond stamp encoded in an entry file name, or None.

    None for anything that does not parse, and the reader treats None as "read
    this file" rather than "skip it": the prefilter may only ever make the scan
    CHEAPER, never decide eligibility. A hand-renamed file that still holds a
    genuine entry must not become invisible."""
    if not name.endswith(ENTRY_SUFFIX):
        return None
    stem = name[:-len(ENTRY_SUFFIX)]
    _, sep, stamp = stem.rpartition(NAME_SEP)
    if not sep or not stamp.isdigit():
        return None
    return int(stamp)


def _as_int(value: Any) -> int | None:
    # `mapping.to_int`'s rule, restated rather than imported: importing
    # `.mapping` from here would be a relative import, and this module is
    # loaded as a TOP-LEVEL module by the addon, where a relative import
    # cannot resolve. The rule is four lines; the `gmssl` chain the split
    # avoids is the whole reason the split exists.
    try:
        if value is None or value == '':
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    # `mapping._flag`'s rule, restated for the reason above. TikTok serves
    # `has_more` as numeric 0/1 and a live reply may render it as the STRINGS
    # '0'/'1' — and bare `bool('0')` is True, which would report an exhausted
    # feed as having more pages. Numbers are compared, not tested for truth.
    number = _as_int(value)
    return bool(number) if number is not None else bool(value)


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    """One `/aweme/v1/aweme/post/` response, as one spool file.

    Frozen per `.claude/rules/code-standards.md`. `aweme_list` is a tuple so
    the whole entry is immutable; `as_json` renders it back as a JSON array."""

    user_id: str
    captured_at: float
    status: str
    reason: str | None
    has_more: bool
    max_cursor: int | None
    status_code: int | None
    path: str
    byte_length: int
    aweme_list: tuple[Mapping[str, Any], ...]

    @property
    def is_ok(self) -> bool:
        """Whether this entry carries a feed the driver may return."""
        return self.status == STATUS_OK

    @classmethod
    def from_response(cls, *, user_id: str, captured_at: float, body: bytes | None,
                      path: str = POST_FEED_PATH) -> 'SpoolEntry':
        """An entry for one live response. Classifies as it builds.

        `body=None` means mitmproxy could not decode the response at all
        (responses are gzip-encoded upstream and `.content` decodes them, but
        a truncated or unknown encoding raises rather than returning bytes).
        That is an unreadable RESPONSE, which is a different thing from no
        response, and the driver must be able to tell them apart."""
        status, reason, payload = _classify(body)
        return cls(user_id=user_id.strip(), captured_at=float(captured_at), status=status,
                   reason=reason, has_more=_as_bool(payload.get(FIELD_HAS_MORE)),
                   max_cursor=_as_int(payload.get(FIELD_MAX_CURSOR)),
                   status_code=_as_int(payload.get(FIELD_STATUS_CODE)), path=path,
                   byte_length=0 if body is None else len(body),
                   aweme_list=_aweme_list(payload))

    @classmethod
    def from_mapping(cls, data: object) -> 'SpoolEntry':
        """One parsed entry file, validated at this boundary.

        Raises `ValueError` on anything unusable so the reader can skip that
        FILE loudly instead of carrying a half entry into a published message.
        The spool is written by our own addon, but it is a file on disk that
        another process wrote, which is the definition of external data."""
        if not isinstance(data, Mapping):
            raise ValueError(f'expected a JSON object, got {type(data).__name__}')
        user_id = data.get(FIELD_USER_ID)
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError(f'{FIELD_USER_ID!r} missing or not a non-empty string')
        captured_at = data.get(FIELD_CAPTURED_AT)
        if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
            raise ValueError(f'{FIELD_CAPTURED_AT!r} missing or not a number')
        status = data.get(FIELD_STATUS)
        if status not in ENTRY_STATUSES:
            raise ValueError(f'{FIELD_STATUS!r} is not one of {", ".join(ENTRY_STATUSES)}')
        raw_list = data.get(FIELD_AWEME_LIST)
        if status == STATUS_OK and not isinstance(raw_list, list):
            # An `ok` entry with no list is a contradiction in the file, not an
            # empty feed. Refusing it here is what keeps "empty means empty"
            # true for every entry the driver does accept.
            raise ValueError(f'{FIELD_STATUS!r} is {STATUS_OK!r} but {FIELD_AWEME_LIST!r} is not a JSON array')
        reason = data.get(FIELD_REASON)
        return cls(user_id=user_id.strip(), captured_at=float(captured_at), status=status,
                   reason=reason if isinstance(reason, str) else None,
                   has_more=_as_bool(data.get(FIELD_HAS_MORE)),
                   max_cursor=_as_int(data.get(FIELD_MAX_CURSOR)),
                   status_code=_as_int(data.get(FIELD_STATUS_CODE)),
                   path=str(data.get(FIELD_PATH) or ''),
                   byte_length=_as_int(data.get(FIELD_BYTE_LENGTH)) or 0,
                   aweme_list=tuple(item for item in (raw_list or []) if isinstance(item, Mapping)))

    def as_json(self) -> str:
        """This entry as the JSON text of one spool file.

        `ensure_ascii=False`, so an Azerbaijani caption stays readable on disk
        instead of becoming `\\uXXXX` escapes — the file is written UTF-8."""
        return json.dumps({FIELD_USER_ID: self.user_id, FIELD_CAPTURED_AT: self.captured_at,
                           FIELD_STATUS: self.status, FIELD_REASON: self.reason,
                           FIELD_HAS_MORE: self.has_more, FIELD_MAX_CURSOR: self.max_cursor,
                           FIELD_STATUS_CODE: self.status_code, FIELD_PATH: self.path,
                           FIELD_BYTE_LENGTH: self.byte_length,
                           FIELD_AWEME_LIST: [dict(item) for item in self.aweme_list]},
                          ensure_ascii=False)


def entry_for_response(*, url: str, captured_at: float, body: bytes | None,
                       path: str = POST_FEED_PATH) -> SpoolEntry:
    """The entry for one live response, keyed by URL param else by the body.

    The key is the URL's `user_id` when the request carried one, else the
    account uid the body names (`account_uid`). The body is parsed ONCE, by
    `from_response`; the fallback reads the already-built `aweme_list`. An
    entry with `user_id == ''` means neither source named an account — the
    caller must not write it, and `write_entry` refuses it anyway."""
    entry = SpoolEntry.from_response(user_id=user_id_from_url(url), captured_at=captured_at,
                                     body=body, path=path)
    if entry.user_id:
        return entry
    return replace(entry, user_id=account_uid(entry.aweme_list))


def _classify(body: bytes | None) -> tuple[str, str | None, Mapping[str, Any]]:
    """`(status, reason, payload)` for one response body."""
    if body is None:
        return STATUS_UNREADABLE, REASON_UNDECODABLE, {}
    if not body:
        # The `tt_orcas_res: 1` shape: HTTP 200, zero bytes. Recorded as a
        # response that arrived and could not be read — never as zero posts.
        return STATUS_UNREADABLE, REASON_EMPTY, {}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return STATUS_UNREADABLE, REASON_NOT_JSON, {}
    if not isinstance(payload, Mapping):
        return STATUS_UNREADABLE, REASON_NOT_OBJECT, {}
    if not isinstance(payload.get(FIELD_AWEME_LIST), list):
        if payload.get(FIELD_STATUS_CODE) == 0 and payload.get(FIELD_STATUS_MSG) == NO_MORE_VIDEOS_MSG:
            # MEASURED (2026-09-14, account with `aweme_count: 0`): the feed of
            # an account with no posts is `{"status_code": 0, "status_msg":
            # "No more videos"}` with NO `aweme_list` key at all. That is an
            # empty feed the server stated explicitly, not a reply we failed to
            # read — treating it as unreadable requeued the job forever.
            return STATUS_OK, None, {**payload, FIELD_AWEME_LIST: []}
        # PRESENT but empty is an ordinary page and stays `ok`. ABSENT without
        # that explicit statement is a reply we did not understand —
        # `.claude/rules/anti-block.md`: an unreadable reply is never reported
        # as an empty success.
        return STATUS_UNREADABLE, REASON_NO_AWEME_LIST, payload
    return STATUS_OK, None, payload


def _aweme_list(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get(FIELD_AWEME_LIST)
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    """One full profile or handle-resolution failure, as one spool file.

    `handle` is the file key: the lower-cased `username` the reply names when
    it is readable, else the request's `sec_user_id` — an error reply names no
    username. Resolver failures use the requested `id` handle instead, because
    the app never fetches a profile for an invalid handle. Every
    counter is None when the reply did not carry it (`signature` never does —
    measured, see `HEADER_COUNTER_KEYS`)."""

    handle: str
    captured_at: float
    status: str
    status_code: int | None
    status_msg: str | None
    user_id: str | None
    sec_uid: str | None
    username: str | None
    nickname: str | None
    avatar_url: str | None
    follower_count: int | None
    following_count: int | None
    aweme_count: int | None
    heart_count: int | None
    signature: str | None
    verified: bool
    private: bool
    region_code: str | None

    @property
    def is_ok(self) -> bool:
        """Whether this entry resolved a handle to an account."""
        return self.status == STATUS_OK and bool(self.user_id)

    @property
    def is_unavailable(self) -> bool:
        """Whether the resolver explicitly rejected this requested handle."""
        return self.status == STATUS_UNAVAILABLE and self.status_code == INVALID_UNIQUE_ID_CODE

    @property
    def is_throttled(self) -> bool:
        """Whether risk control refused the resolver for this device (0-byte reply)."""
        return self.status == STATUS_THROTTLED

    @classmethod
    def from_resolver_response(cls, *, url: str, captured_at: float,
                               body: bytes | None) -> 'ProfileEntry | None':
        """Capture a handle resolver failure; a success awaits the full profile.

        The request's handle is essential: an invalid-handle reply contains
        no user object. Only the measured rejection code proves absence;
        malformed or unfamiliar replies remain ordinary, retryable errors.
        """
        if body == b'':
            handle = _param_from_url(url, UNIQUE_ID_PARAM).strip().lower()
            return cls(handle=handle, captured_at=float(captured_at), status=STATUS_THROTTLED, status_code=None,
                       status_msg=None, user_id=None, sec_uid=None, username=None, nickname=None, avatar_url=None,
                       follower_count=None, following_count=None, aweme_count=None, heart_count=None,
                       signature=None, verified=False, private=False, region_code=None)
        payload = _json_object(body)
        raw_code = payload.get(FIELD_STATUS_CODE)
        # Do not truncate a malformed float or coerce bool into a status code.
        status_code = _as_int(raw_code) if isinstance(raw_code, (str, int)) and not isinstance(raw_code, bool) else None
        if status_code != INVALID_UNIQUE_ID_CODE:
            # Only the CONFIRMED rejection is worth a profile entry. Anything
            # else — a success, an unfamiliar code, an unreadable body
            # (measured 2026-09-17: `status_code` absent, both instances,
            # `@bakidyp`) — must not be written under the handle, because the
            # driver reads the newest entry for the handle as THE profile
            # answer and would fail the visit before the real profile reply
            # (which follows within a second) ever lands.
            return None
        handle = _param_from_url(url, UNIQUE_ID_PARAM).strip().lower()
        status = STATUS_UNAVAILABLE
        return cls(handle=handle, captured_at=float(captured_at), status=status, status_code=status_code,
                   status_msg=_str_or_none(payload.get(FIELD_STATUS_MSG)), user_id=None, sec_uid=None,
                   username=None, nickname=None, avatar_url=None, follower_count=None,
                   following_count=None, aweme_count=None, heart_count=None, signature=None,
                   verified=False, private=False, region_code=None)

    @classmethod
    def from_response(cls, *, url: str, captured_at: float, body: bytes | None) -> 'ProfileEntry':
        """The entry for one live profile response, keyed by username else `sec_user_id`."""
        payload = _json_object(body)
        status_code = _as_int(payload.get(FIELD_STATUS_CODE))
        status_msg = _str_or_none(payload.get(FIELD_STATUS_MSG))
        common = _mapping(payload.get(COMMON_KEY))
        info = _mapping(common.get(PROFILE_INFO_KEY))
        username = _str_or_none(info.get(USERNAME_KEY))
        uid = _as_int(info.get(UID_KEY))
        if status_code != 0 or username is None or uid is None:
            return cls(handle=username.lower() if username else sec_user_id_from_url(url),
                       captured_at=float(captured_at), status=STATUS_ERROR, status_code=status_code,
                       status_msg=status_msg, user_id=str(uid) if uid is not None else None, sec_uid=None,
                       username=username, nickname=None, avatar_url=None, follower_count=None,
                       following_count=None, aweme_count=None, heart_count=None, signature=None,
                       verified=False, private=False, region_code=None)
        statics = _mapping(common.get(STATICS_INFO_KEY))
        account = _mapping(common.get(ACCOUNT_INFO_KEY))
        header = _header_counters(payload.get(HEADER_KEY))
        region = _str_or_none(header.get(REGION_KEY))
        return cls(handle=username.lower(), captured_at=float(captured_at), status=STATUS_OK,
                   status_code=status_code, status_msg=status_msg, user_id=str(uid),
                   sec_uid=_str_or_none(info.get(SEC_UID_KEY)), username=username,
                   nickname=_str_or_none(info.get(NICKNAME_KEY)), avatar_url=_avatar_url(info.get(AVATAR_KEY)),
                   follower_count=_as_int(header.get(FOLLOWER_COUNT_KEY)),
                   following_count=_as_int(header.get(FOLLOWING_COUNT_KEY)),
                   aweme_count=_as_int(statics.get(AWEME_COUNT_KEY)),
                   heart_count=_as_int(header.get(LIKE_COUNT_KEY)), signature=None,
                   verified=_as_bool(account.get(HAS_CERT_KEY)), private=_as_bool(account.get(PRIVATE_ACCOUNT_KEY)),
                   region_code=region.upper() if region else None)

    @classmethod
    def from_mapping(cls, data: object) -> 'ProfileEntry':
        """One parsed profile file, validated at this boundary (`ValueError` when unusable)."""
        if not isinstance(data, Mapping):
            raise ValueError(f'expected a JSON object, got {type(data).__name__}')
        handle = data.get(FIELD_HANDLE)
        if not isinstance(handle, str) or not handle.strip():
            raise ValueError(f'{FIELD_HANDLE!r} missing or not a non-empty string')
        captured_at = data.get(FIELD_CAPTURED_AT)
        if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
            raise ValueError(f'{FIELD_CAPTURED_AT!r} missing or not a number')
        status = data.get(FIELD_STATUS)
        if status not in PROFILE_STATUSES:
            raise ValueError(f'{FIELD_STATUS!r} is not one of {", ".join(PROFILE_STATUSES)}')
        user_id = _as_int(data.get(FIELD_USER_ID))
        return cls(handle=handle.strip(), captured_at=float(captured_at), status=status,
                   status_code=_as_int(data.get(FIELD_STATUS_CODE)), status_msg=_str_or_none(data.get(FIELD_STATUS_MSG)),
                   user_id=str(user_id) if user_id is not None else None,
                   sec_uid=_str_or_none(data.get('sec_uid')), username=_str_or_none(data.get('username')),
                   nickname=_str_or_none(data.get('nickname')), avatar_url=_str_or_none(data.get('avatar_url')),
                   follower_count=_as_int(data.get('follower_count')), following_count=_as_int(data.get('following_count')),
                   aweme_count=_as_int(data.get('aweme_count')), heart_count=_as_int(data.get('heart_count')),
                   signature=_str_or_none(data.get('signature')), verified=_as_bool(data.get('verified')),
                   private=_as_bool(data.get('private')), region_code=_str_or_none(data.get('region_code')))

    def as_json(self) -> str:
        """This entry as the JSON text of one profile spool file."""
        return json.dumps(asdict(self), ensure_ascii=False)


def _json_object(body: bytes | None) -> Mapping[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_or_none(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _avatar_url(avatar: Any) -> str | None:
    """The first URL of the first populated avatar variant, or None."""
    avatar = _mapping(avatar)
    for variant in AVATAR_VARIANTS:
        urls = _mapping(avatar.get(variant)).get(URL_LIST_KEY)
        if isinstance(urls, list):
            for url in urls:
                if isinstance(url, str) and url:
                    return url
    return None


def _header_counters(header: Any) -> dict[str, Any]:
    """`HEADER_COUNTER_KEYS` values found in any `biz_data` object under `header`.

    First occurrence of each key wins; depth-bounded walk over dicts and lists."""
    found: dict[str, Any] = {}
    stack: list[tuple[Any, int]] = [(header, 0)]
    while stack and len(found) < len(HEADER_COUNTER_KEYS):
        node, depth = stack.pop()
        if depth > MAX_HEADER_DEPTH:
            continue
        if isinstance(node, Mapping):
            biz = node.get(BIZ_DATA_KEY)
            if isinstance(biz, Mapping):
                for key in HEADER_COUNTER_KEYS:
                    if key not in found and biz.get(key) is not None:
                        found[key] = biz[key]
            stack.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
    return found


def write_entry(spool_dir: str, entry: SpoolEntry) -> str:
    """Write `entry` into `spool_dir` atomically; return the path written.

    Temp file in the SAME directory (so `os.replace` is a rename inside one
    filesystem and therefore atomic) then `os.replace`. A reader can only ever
    see the whole entry or no file at all — the invariant
    `.claude/rules/learned-lessons.md` records for `identities.json`, and the
    reason the driver may parse a spool file the moment it appears."""
    return _write_json(spool_dir, key=file_key(entry.user_id), captured_at=entry.captured_at,
                       text=entry.as_json(), what='user_id')


def write_profile_entry(spool_dir: str, entry: 'ProfileEntry') -> str:
    """Write a profile `entry` under `<spool_dir>/profiles/` atomically (see `write_entry`)."""
    return _write_json(profiles_dir(spool_dir), key=file_key(entry.handle), captured_at=entry.captured_at,
                       text=entry.as_json(), what='handle')


def profiles_dir(spool_dir: str) -> str:
    """Where profile entries live: a sub-directory of the posts spool."""
    return os.path.join(spool_dir, PROFILES_DIRNAME)


def _write_json(directory: str, *, key: str, captured_at: float, text: str, what: str) -> str:
    if not key:
        raise ValueError(f'entry has no usable {what} to key a file by')
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, _free_name(directory, key, captured_at))
    handle_fd, temp_path = tempfile.mkstemp(dir=directory, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(handle_fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except BaseException:
        # A temp file left behind would accumulate silently and, being
        # `.tmp`-suffixed, would never be read either. Cleanup failure is
        # swallowed on purpose: the original exception is the one worth having.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return target


def _free_name(directory: str, key: str, captured_at: float) -> str:
    stamp = int(captured_at * NANOS_PER_SECOND)
    for offset in range(MAX_NAME_ATTEMPTS):
        name = f'{key}{NAME_SEP}{stamp + offset}{ENTRY_SUFFIX}'
        if not os.path.exists(os.path.join(directory, name)):
            return name
    raise OSError(f'no free entry name for {key} after {MAX_NAME_ATTEMPTS} attempts')


class FileSpool:
    """A spool directory, as the device driver reads it.

    Two operations, and the driver uses BOTH on every visit — see
    `device/driver.py` for why one is not enough:

    * `entry_names` is the CENSUS, taken before the intent is fired. It is
      clock-free, so it holds an entry from a previous visit out of the running
      whatever the two processes' clocks say.
    * `entries_since` applies the census and the timestamp together and
      returns EVERY eligible entry — one profile visit makes the app fetch two
      posts pages (measured: `count=9` then `count=18`), and the driver merges
      them. `newest_since` is the single-entry view of the same rule.

    Injected into the driver as an object so a unit test can substitute a fake
    and no test needs a Waydroid container or a mitmproxy."""

    def __init__(self, spool_dir: str) -> None:
        self._dir = spool_dir

    @property
    def path(self) -> str:
        """The directory this spool reads."""
        return self._dir

    def entry_names(self, user_id: str) -> frozenset[str]:
        """Every entry file name currently present for `user_id`.

        An absent directory is an empty set, not an error: the addon may not
        have spooled anything yet, and a driver that refused to start until it
        had would be broken on a clean checkout."""
        key = file_key(user_id)
        if not key:
            return frozenset()
        return _names(self._dir, prefix=f'{key}{NAME_SEP}')

    def all_entry_names(self) -> frozenset[str]:
        """Every entry file name in the spool, for ANY account.

        The census `visit_handle` takes before firing: the visit does not know
        the account's uid yet (the profile reply is what tells it), so the whole
        directory is snapshotted and the uid filter is applied by the poll."""
        return _names(self._dir, prefix='')

    def entries_since(self, user_id: str, *, after: float,
                      exclude: Collection[str] = ()) -> dict[str, SpoolEntry]:
        """Every ELIGIBLE entry for `user_id`, file name -> entry, oldest first.

        Eligible means all three of:

        1. the file name is not in `exclude` — the census taken before the
           visit, which is the clock-free half of the stale-entry guard, plus
           whatever the driver has already merged this visit;
        2. the entry's own `captured_at` is at or after `after`, which rejects
           a previous visit's response that landed between the census and the
           intent — the census cannot see that one;
        3. the entry's own `user_id` equals the one asked for, so a
           percent-encoding collision in a file name can never serve one
           account's feed as another's.

        Keyed by FILE NAME so the driver can extend its exclusion set with what
        it has merged and never read the same entry twice. Empty means nothing
        eligible is there YET — not an error and not an empty feed."""
        found: list[tuple[str, SpoolEntry]] = []
        after_nanos = int(after * NANOS_PER_SECOND)
        for name in sorted(self.entry_names(user_id)):
            if name in exclude:
                continue
            stamp = nanos_from_name(name)
            if stamp is not None and stamp < after_nanos:
                # Prefilter only — cheaper, never decisive. An unparseable
                # name falls through to a full read.
                continue
            entry = self._read(name)
            if entry is None or entry.user_id != user_id.strip() or entry.captured_at < after:
                continue
            found.append((name, entry))
        found.sort(key=lambda pair: pair[1].captured_at)
        return dict(found)

    def newest_since(self, user_id: str, *, after: float,
                     exclude: Collection[str] = ()) -> SpoolEntry | None:
        """The newest eligible entry for `user_id` (`entries_since`'s rule), or None."""
        entries = self.entries_since(user_id, after=after, exclude=exclude)
        return max(entries.values(), key=lambda entry: entry.captured_at, default=None)

    def _read(self, name: str) -> SpoolEntry | None:
        return _read_entry(self._dir, name, SpoolEntry.from_mapping)


class ProfileSpool:
    """`<spool_dir>/profiles/`, as the device driver reads it.

    Mirrors `FileSpool` — the same census + timestamp + own-key rule — keyed
    by HANDLE: the caller passes the handle as typed, and it is lower-cased
    and `file_key`-encoded here, exactly as the addon keys what it writes."""

    def __init__(self, spool_dir: str) -> None:
        self._dir = profiles_dir(spool_dir)

    @property
    def path(self) -> str:
        """The directory this spool reads."""
        return self._dir

    def entry_names(self, handle: str) -> frozenset[str]:
        """Every profile entry file name currently present for `handle`."""
        key = file_key(handle.lower())
        if not key:
            return frozenset()
        return _names(self._dir, prefix=f'{key}{NAME_SEP}')

    def newest_since(self, handle: str, *, after: float,
                     exclude: Collection[str] = ()) -> ProfileEntry | None:
        """The newest eligible profile entry for `handle` (`FileSpool.entries_since`'s rule), or None."""
        wanted = handle.strip().lower()
        after_nanos = int(after * NANOS_PER_SECOND)
        newest: ProfileEntry | None = None
        for name in self.entry_names(wanted):
            if name in exclude:
                continue
            stamp = nanos_from_name(name)
            if stamp is not None and stamp < after_nanos:
                continue
            entry = _read_entry(self._dir, name, ProfileEntry.from_mapping)
            if entry is None or entry.handle != wanted or entry.captured_at < after:
                continue
            if newest is None or entry.captured_at >= newest.captured_at:
                newest = entry
        return newest


def _names(directory: str, *, prefix: str) -> frozenset[str]:
    """Entry file names in `directory` starting with `prefix`; absent dir = empty."""
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        logger.debug('spool directory %s does not exist yet', directory)
        return frozenset()
    except OSError as exc:
        logger.warning('cannot list spool directory %s: %s', directory, exc)
        return frozenset()
    return frozenset(name for name in names if name.startswith(prefix) and name.endswith(ENTRY_SUFFIX))


def _read_entry(directory: str, name: str, parse: Callable[[object], Any]) -> Any | None:
    full = os.path.join(directory, name)
    try:
        with open(full, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        # The file NAME and the error, never the content: an entry holds a
        # whole feed. A single unusable file must not fail a visit that
        # another file could still serve.
        logger.warning('skipping unusable spool entry %s: %s', name, exc)
        return None
    try:
        return parse(payload)
    except ValueError as exc:
        logger.warning('skipping unusable spool entry %s: %s', name, exc)
        return None
