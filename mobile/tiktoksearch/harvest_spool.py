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
the percent-encoded `user_id` the request carried:

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
from dataclasses import dataclass
from typing import Any, Collection, Mapping
from urllib.parse import parse_qsl, quote, urlsplit

logger = logging.getLogger('tiktoksearch.harvest_spool')

# The one path this spool is about. The account's own post feed, on the
# `api32-core-alisg.tiktokv.com` gateway — NOT the `search19-normal` host the
# signed client uses, and not `/aweme/v1/user/profile/other/`.
POST_FEED_PATH = '/aweme/v1/aweme/post/'
# The request param the entry is keyed by. The app sends the profile owner's
# public uid here, which is the same value `POST /profile` answers with, and is
# how the driver finds "the newest response for this user".
USER_ID_PARAM = 'user_id'

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
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name == USER_ID_PARAM:
            return value.strip()
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
        # PRESENT but empty is an ordinary page and stays `ok`. ABSENT is a
        # reply we did not understand — `.claude/rules/anti-block.md`: an
        # unreadable reply is never reported as an empty success.
        return STATUS_UNREADABLE, REASON_NO_AWEME_LIST, payload
    return STATUS_OK, None, payload


def _aweme_list(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get(FIELD_AWEME_LIST)
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def write_entry(spool_dir: str, entry: SpoolEntry) -> str:
    """Write `entry` into `spool_dir` atomically; return the path written.

    Temp file in the SAME directory (so `os.replace` is a rename inside one
    filesystem and therefore atomic) then `os.replace`. A reader can only ever
    see the whole entry or no file at all — the invariant
    `.claude/rules/learned-lessons.md` records for `identities.json`, and the
    reason the driver may parse a spool file the moment it appears."""
    key = file_key(entry.user_id)
    if not key:
        raise ValueError('entry has no usable user_id to key a file by')
    os.makedirs(spool_dir, exist_ok=True)
    target = os.path.join(spool_dir, _free_name(spool_dir, entry))
    handle_fd, temp_path = tempfile.mkstemp(dir=spool_dir, prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(handle_fd, 'w', encoding='utf-8') as handle:
            handle.write(entry.as_json())
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


def _free_name(spool_dir: str, entry: SpoolEntry) -> str:
    stamp = int(entry.captured_at * NANOS_PER_SECOND)
    key = file_key(entry.user_id)
    for offset in range(MAX_NAME_ATTEMPTS):
        name = f'{key}{NAME_SEP}{stamp + offset}{ENTRY_SUFFIX}'
        if not os.path.exists(os.path.join(spool_dir, name)):
            return name
    raise OSError(f'no free entry name for {key} after {MAX_NAME_ATTEMPTS} attempts')


class FileSpool:
    """A spool directory, as the device driver reads it.

    Two operations, and the driver uses BOTH on every visit — see
    `device/driver.py` for why one is not enough:

    * `entry_names` is the CENSUS, taken before the intent is fired. It is
      clock-free, so it holds an entry from a previous visit out of the running
      whatever the two processes' clocks say.
    * `newest_since` applies the census and the timestamp together.

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
        prefix = f'{key}{NAME_SEP}'
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            logger.debug('spool directory %s does not exist yet', self._dir)
            return frozenset()
        except OSError as exc:
            logger.warning('cannot list spool directory %s: %s', self._dir, exc)
            return frozenset()
        return frozenset(name for name in names
                         if name.startswith(prefix) and name.endswith(ENTRY_SUFFIX))

    def newest_since(self, user_id: str, *, after: float,
                     exclude: Collection[str] = ()) -> SpoolEntry | None:
        """The newest ELIGIBLE entry for `user_id`, or None.

        Eligible means all three of:

        1. the file name is not in `exclude` — the census taken before the
           visit, which is the clock-free half of the stale-entry guard;
        2. the entry's own `captured_at` is at or after `after`, which rejects
           a previous visit's response that landed between the census and the
           intent — the census cannot see that one;
        3. the entry's own `user_id` equals the one asked for, so a
           percent-encoding collision in a file name can never serve one
           account's feed as another's.

        None means nothing eligible is there YET. It is not an error and it is
        not an empty feed: an entry whose `aweme_list` is empty is returned as
        an entry."""
        best: SpoolEntry | None = None
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
            if best is None or entry.captured_at > best.captured_at:
                best = entry
        return best

    def _read(self, name: str) -> SpoolEntry | None:
        full = os.path.join(self._dir, name)
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
            return SpoolEntry.from_mapping(payload)
        except ValueError as exc:
            logger.warning('skipping unusable spool entry %s: %s', name, exc)
            return None
