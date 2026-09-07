"""Opaque, HMAC-authenticated cross-request pagination token.

TikTok's search is a SESSION: a request at offset > 0 must echo the previous
response's `search_id` (`log_pb.impr_id`) or the API answers
`search_nil_item: empty_session`. A stateless HTTP API therefore has to hand
that session back to the caller, which is what this token is for. It carries,
per endpoint, TikTok's own cursor, the `search_id`, whether that endpoint has
more to give and whether it was ever started — plus a stable, non-secret handle
for the device that owns the session (the session is assumed device-bound, so
the next page must be served by the same identity) and a bounded window of
recent record fingerprints for cross-page dedup.

Three properties matter and are enforced here:

* **Authenticated.** The token is signed with an HMAC over the whole payload
  using a secret generated with `secrets.token_bytes` at import time. It is
  per-process and never persisted, so a caller cannot mint or tamper with a
  token — pinning a device is not caller-assertable, a forged pin cannot be
  used to drive one warm identity into `stale`, and the dedup window cannot be
  edited to make the service re-serve or hide records. Tokens consequently do
  not survive a server restart, which is correct: a TikTok search session is
  short-lived anyway.
* **Secret-free.** The payload holds no `device_id`, `iid`, `cookie` or
  `x_tt_token`. The device is named by `hmac(secret, identity_key)[:16]`, a
  stable handle that survives identity-file reordering (unlike a positional
  label, which can silently repoint at a different device) and — being keyed —
  is not a brute-forceable commitment to a 19-digit `device_id` the way a bare
  `sha256` would be.
* **Self-bounded.** `MAX_PAGE_TOKEN_CHARS` is DERIVED from the worst-case
  token this module can build, and `encode` checks the wire length against it,
  so the server can never mint a token that `decode` would then refuse.

`q` binds the token to its originating query so it cannot be replayed against a
different search. Every validation failure is a `ValueError` — a client error
(422), never a `SoftError`/502.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import string
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

TOKEN_VERSION = 1

# Wire format: base64url(payload_json) "." base64url(hmac-sha256 tag).
_SEPARATOR = '.'
_SEPARATOR_PARTS = 2
_JSON_SEPARATORS = (',', ':')
_KEY_VERSION = 'v'
_KEY_QUERY_HASH = 'q'
_KEY_DEVICE = 'dev'
_KEY_ENDPOINTS = 'eps'
_KEY_SEEN = 'seen'
_KEY_PATH = 'p'
_KEY_CURSOR = 'c'
_KEY_SEARCH_ID = 'sid'
_KEY_HAS_MORE = 'm'
_KEY_STARTED = 's'

# Per-process token-signing secret. Generated once at import, never written to
# disk, never logged, never in config or the environment.
_SECRET_BYTES = 32
_TAG_BYTES = 16
_TOKEN_SECRET = secrets.token_bytes(_SECRET_BYTES)

# Hard bounds. A token is caller-supplied input on the path to a SIGNED TikTok
# URL served by a live warm identity, so every field is bounded before it can
# reach `client.py`.
MAX_ENDPOINT_CURSOR = 100_000
MAX_ENDPOINTS = 4
MAX_SEARCH_ID_CHARS = 128
MAX_ENDPOINT_PATH_CHARS = 64
_SEARCH_ID_ALLOWED = frozenset(string.ascii_letters + string.digits + '-_')
_QUERY_HASH_CHARS = 32
_DEVICE_HANDLE_CHARS = 16
_B64_BLOCK = 4

# Cross-request dedup state. The `seen` set inside client.py is per-REQUEST;
# cross-request dedup used to rest on per-endpoint cursor monotonicity alone,
# which only holds while ONE endpoint drives the stream. As soon as `limit`
# exceeds the primary endpoint's ~35-record depth, page 1 opens both video
# endpoints, so page 2 resumes `search/item/` independently and its deeper
# window can re-emit videos the primary already served — and a caller that
# concatenates pages (demo.html does) shows them twice.
#
# So the token carries a sliding window of the most recent record-id
# fingerprints and the next request seeds `seen` from it. Exact, not
# probabilistic: 4 bytes of sha256 per id. A 128-byte Bloom filter would fit in
# a tenth of the space, but a false positive silently DROPS a genuine record
# with no way for anyone to notice, which is a worse failure than a slightly
# larger opaque token.
#
# The window is measured in RECORDS, not pages: 60 x 4 covers four pages only
# at the shipped `max_results_per_search` of 60. A profile that raises that cap
# keeps the same 240 fingerprints and so silently narrows the coverage to fewer
# pages (two, at a 120-record page) — raise `_WINDOW_PAGES` alongside such a
# profile change, or accept that records older than 240 can be re-emitted. The
# entries that fall off the back are, at the shipped size, the ones neither
# endpoint can still re-emit anyway.
FINGERPRINT_BYTES = 4
_PAGE_RECORD_BUDGET = 60
_WINDOW_PAGES = 4
MAX_SEEN_FINGERPRINTS = _PAGE_RECORD_BUDGET * _WINDOW_PAGES

# Deliberately terse, non-leaking messages: they are returned to HTTP clients.
_MALFORMED = 'malformed page_token'
_UNTRUSTED = (
    'page_token is not valid (tampered, or minted by a previous server run) — '
    'start a new search without page_token.'
)
_UNSUPPORTED = 'unsupported page_token version'
_QUERY_MISMATCH = 'page_token does not belong to this query'
# Unreachable: MAX_PAGE_TOKEN_CHARS is derived from the worst case below and
# `encode` sheds dedup fingerprints before it could ever get here. Kept as a
# hard stop so a future field cannot quietly mint a token decode would refuse.
_TOO_LONG = 'page_token could not be minted within its size bound'


@dataclass(frozen=True, slots=True)
class EndpointState:
    """Where one search endpoint left off.

    `cursor` is TikTok's own cursor (authoritative — it diverges from
    `start_cursor + len(records)` whenever dedup drops an item) and `search_id`
    is the session handle the next request must echo.

    `started` and `has_more` are explicit because `cursor == 0` is ambiguous on
    its own: it means both "resume at the beginning" and "never queried". The
    combinations that occur:

    * `started=False, has_more=True`  — not yet opened, still openable. A later
      request MAY open it: `seen` is seeded from the token's fingerprint
      window, so its overlapping first window is deduped rather than re-served.
    * `started=True,  has_more=True`  — resumable: re-query with cursor+session.
    * `started=True,  has_more=False` — exhausted: never re-query, or it returns
      a tail empty and gets misread as risk-control.
    * `started=False, has_more=False` — TikTok answered has_more=false on the
      very first window, so there was never anything to open.
    """
    path: str
    cursor: int
    search_id: str
    has_more: bool = True
    started: bool = False


@dataclass(frozen=True, slots=True)
class PageToken:
    version: int
    query_hash: str
    device_handle: str
    endpoints: tuple[EndpointState, ...]
    # Fingerprints of the record ids this stream has already served, oldest
    # first. Bounded by MAX_SEEN_FINGERPRINTS; inside the HMAC, so a caller can
    # neither forge nor strip it.
    seen: tuple[bytes, ...] = ()

    def state_for(self, path: str) -> EndpointState | None:
        """The stored end state for `path`, or None if the token has none."""
        for endpoint in self.endpoints:
            if endpoint.path == path:
                return endpoint
        return None


class SeenWindow:
    """Dedup set for record keys, with a bounded, order-preserving tail.

    Membership is tested on a 4-byte sha256 fingerprint rather than the raw id
    so that the same structure serves both halves of the job: exact in-request
    dedup across the two video endpoints, and the bounded cross-request window
    that rides in the page token. 32 bits over a few hundred ids is a ~1e-8
    collision risk per lookup — far below the rate at which TikTok itself
    duplicates results."""

    __slots__ = ('_order', '_seen')

    def __init__(self, fingerprints: Iterable[bytes] = ()) -> None:
        self._order: list[bytes] = []
        self._seen: set[bytes] = set()
        for fingerprint in fingerprints:
            self._remember(fingerprint)

    def add(self, key: str) -> bool:
        """Remember `key`. False (and no change) if it was already present."""
        digest = fingerprint(key)
        if digest in self._seen:
            return False
        self._remember(digest)
        return True

    def recent(self) -> tuple[bytes, ...]:
        """The newest MAX_SEEN_FINGERPRINTS fingerprints, oldest first — what
        goes into the next page token."""
        return tuple(self._order[-MAX_SEEN_FINGERPRINTS:])

    def _remember(self, digest: bytes) -> None:
        self._seen.add(digest)
        self._order.append(digest)


def fingerprint(key: str) -> bytes:
    """Short, stable fingerprint of a record id / username."""
    return hashlib.sha256(key.encode('utf-8')).digest()[:FINGERPRINT_BYTES]


def device_handle(identity_key: str) -> str:
    """Stable, non-secret handle for the device that owns a search session.

    Keyed with the per-process token secret, so it is stable for the lifetime
    of the process (which is all a search session needs) without being a
    guessable commitment to the underlying value: a bare `sha256` of a 19-digit
    `device_id` is brute-forceable in seconds, an HMAC under an unknown 32-byte
    key is not. Hashing also means a reordered/rewritten identities.json cannot
    make the handle name a DIFFERENT device the way a positional label can."""
    return hmac.new(
        _TOKEN_SECRET, identity_key.encode('utf-8'), hashlib.sha256
    ).hexdigest()[:_DEVICE_HANDLE_CHARS]


def query_hash(kind: str, term: str, filter_params: Mapping[str, str]) -> str:
    """Stable fingerprint of the search identity a token belongs to.

    Covers the search kind, the term and the filter params — not `limit` or
    `fan_out`, which a caller may legitimately change between pages."""
    payload = json.dumps(
        [kind, term, sorted(filter_params.items())],
        separators=_JSON_SEPARATORS, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:_QUERY_HASH_CHARS]


def sanitize_search_id(value: object) -> str:
    """A TikTok `impr_id`/`logid` reduced to something a token can carry, or ''.

    `decode` bounds `sid` by length and charset because it ends up in a signed
    TikTok URL. The value arrives from upstream, so the SAME bounds have to be
    applied on the way in — otherwise an unusual `impr_id` would be minted into
    a token that the very next request rejects with 422. Callers keep their
    previous session id when this returns '' (`clean or search_id`)."""
    if not isinstance(value, str) or not value:
        return ''
    if len(value) > MAX_SEARCH_ID_CHARS or not _SEARCH_ID_ALLOWED.issuperset(value):
        return ''
    return value


def encode(token: PageToken) -> str:
    """Serialise and sign `token`.

    Guaranteed never to exceed MAX_PAGE_TOKEN_CHARS: that bound is derived from
    the worst case this function can produce, and the dedup window — the only
    droppable state — is shed oldest-first if anything ever pushed past it."""
    trimmed = token
    if len(trimmed.seen) > MAX_SEEN_FINGERPRINTS:
        # decode() refuses a window past this bound, so mint cannot exceed it
        # either — keep the NEWEST entries, they are the ones still re-emittable.
        trimmed = replace(trimmed, seen=trimmed.seen[-MAX_SEEN_FINGERPRINTS:])
    raw = _wire(trimmed)
    while len(raw) > MAX_PAGE_TOKEN_CHARS and trimmed.seen:
        trimmed = replace(trimmed, seen=trimmed.seen[1:])
        raw = _wire(trimmed)
    if len(raw) > MAX_PAGE_TOKEN_CHARS:
        raise ValueError(_TOO_LONG)
    return raw


def decode(raw: str, *, expected_query_hash: str, allowed_paths: Iterable[str]) -> PageToken:
    """Parse and validate a token. Raises ValueError (→ 422) on any problem.

    The HMAC is checked FIRST, so an untrusted blob is never base64-decoded or
    JSON-parsed and no bound below can be reached by a hostile caller."""
    body = _authenticated_body(raw)
    payload = _decode_payload(body)
    version = payload.get(_KEY_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(_MALFORMED)
    if version != TOKEN_VERSION:
        raise ValueError(_UNSUPPORTED)
    hash_value = payload.get(_KEY_QUERY_HASH)
    handle = payload.get(_KEY_DEVICE)
    endpoints = payload.get(_KEY_ENDPOINTS)
    if not isinstance(hash_value, str) or not isinstance(handle, str) or not handle:
        raise ValueError(_MALFORMED)
    if len(handle) != _DEVICE_HANDLE_CHARS:
        raise ValueError(_MALFORMED)
    if not isinstance(endpoints, list) or not endpoints or len(endpoints) > MAX_ENDPOINTS:
        raise ValueError(_MALFORMED)
    if hash_value != expected_query_hash:
        raise ValueError(_QUERY_MISMATCH)
    known = frozenset(allowed_paths)
    return PageToken(
        version=version, query_hash=hash_value, device_handle=handle,
        endpoints=tuple(_endpoint_from(item, known) for item in endpoints),
        seen=_seen_from(payload.get(_KEY_SEEN)))


def _b64encode(blob: bytes) -> str:
    return base64.urlsafe_b64encode(blob).decode('ascii').rstrip('=')


def _b64decode(text: str) -> bytes:
    padded = text + '=' * (-len(text) % _B64_BLOCK)
    try:
        return base64.urlsafe_b64decode(padded.encode('ascii'))
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ValueError(_MALFORMED) from exc


def _tag(body: str) -> bytes:
    return hmac.new(_TOKEN_SECRET, body.encode('ascii'), hashlib.sha256).digest()[:_TAG_BYTES]


def _wire(token: PageToken) -> str:
    """The signed wire string for `token`, with no size check — `encode` owns
    that, and MAX_PAGE_TOKEN_CHARS is derived by calling this on a worst case."""
    body = _b64encode(json.dumps(
        {
            _KEY_VERSION: token.version,
            _KEY_QUERY_HASH: token.query_hash,
            _KEY_DEVICE: token.device_handle,
            _KEY_ENDPOINTS: [
                {
                    _KEY_PATH: endpoint.path,
                    _KEY_CURSOR: endpoint.cursor,
                    _KEY_SEARCH_ID: endpoint.search_id,
                    _KEY_HAS_MORE: endpoint.has_more,
                    _KEY_STARTED: endpoint.started,
                }
                for endpoint in token.endpoints
            ],
            _KEY_SEEN: _b64encode(b''.join(token.seen)),
        },
        separators=_JSON_SEPARATORS, ensure_ascii=False).encode('utf-8'))
    return body + _SEPARATOR + _b64encode(_tag(body))


def _authenticated_body(raw: str) -> str:
    """Split `body.tag`, verify the tag, return the body. Nothing else parses
    the token until this has passed."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PAGE_TOKEN_CHARS:
        raise ValueError(_MALFORMED)
    parts = raw.split(_SEPARATOR)
    if len(parts) != _SEPARATOR_PARTS or not parts[0] or not parts[1]:
        raise ValueError(_MALFORMED)
    body, tag = parts
    try:
        body.encode('ascii')
    except UnicodeEncodeError as exc:
        raise ValueError(_MALFORMED) from exc
    if not hmac.compare_digest(_b64decode(tag), _tag(body)):
        raise ValueError(_UNTRUSTED)
    return body


def _decode_payload(body: str) -> dict[str, object]:
    try:
        payload = json.loads(_b64decode(body).decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(_MALFORMED) from exc
    if not isinstance(payload, dict):
        raise ValueError(_MALFORMED)
    return payload


def _seen_from(value: object) -> tuple[bytes, ...]:
    """Split the dedup window back into fixed-width fingerprints."""
    if value is None:
        return ()
    if not isinstance(value, str):
        raise ValueError(_MALFORMED)
    if not value:
        return ()
    blob = _b64decode(value)
    if len(blob) % FINGERPRINT_BYTES:
        raise ValueError(_MALFORMED)
    if len(blob) > MAX_SEEN_FINGERPRINTS * FINGERPRINT_BYTES:
        raise ValueError(_MALFORMED)
    return tuple(blob[i:i + FINGERPRINT_BYTES]
                 for i in range(0, len(blob), FINGERPRINT_BYTES))


def _endpoint_from(item: object, allowed_paths: frozenset[str]) -> EndpointState:
    if not isinstance(item, dict):
        raise ValueError(_MALFORMED)
    path = item.get(_KEY_PATH)
    cursor = item.get(_KEY_CURSOR)
    search_id = item.get(_KEY_SEARCH_ID)
    has_more = item.get(_KEY_HAS_MORE)
    started = item.get(_KEY_STARTED)
    # `path` goes into a signed TikTok URL: only the endpoints this service
    # actually knows are acceptable. The length bound is redundant against that
    # whitelist but keeps the derived size cap below honest.
    if not isinstance(path, str) or path not in allowed_paths:
        raise ValueError(_MALFORMED)
    if len(path) > MAX_ENDPOINT_PATH_CHARS:
        raise ValueError(_MALFORMED)
    if not isinstance(cursor, int) or isinstance(cursor, bool):
        raise ValueError(_MALFORMED)
    if cursor < 0 or cursor > MAX_ENDPOINT_CURSOR:
        raise ValueError(_MALFORMED)
    # `search_id` is echoed into the signed query string, so bound its length
    # and charset rather than passing an arbitrary caller string through.
    if not isinstance(search_id, str) or len(search_id) > MAX_SEARCH_ID_CHARS:
        raise ValueError(_MALFORMED)
    if search_id and not _SEARCH_ID_ALLOWED.issuperset(search_id):
        raise ValueError(_MALFORMED)
    if not isinstance(has_more, bool) or not isinstance(started, bool):
        raise ValueError(_MALFORMED)
    return EndpointState(path=path, cursor=cursor, search_id=search_id,
                         has_more=has_more, started=started)


def _worst_case() -> PageToken:
    """The largest token this module can emit: every bound at its maximum.

    Every field's serialised length is monotone in the field, and base64 is
    monotone in byte length, so the wire form of ANY real token is no longer
    than the wire form of this one. `has_more`/`started` are False because
    'false' is a character longer than 'true'."""
    return PageToken(
        version=TOKEN_VERSION,
        query_hash='f' * _QUERY_HASH_CHARS,
        device_handle='f' * _DEVICE_HANDLE_CHARS,
        endpoints=tuple(
            EndpointState(path='p' * MAX_ENDPOINT_PATH_CHARS, cursor=MAX_ENDPOINT_CURSOR,
                          search_id='s' * MAX_SEARCH_ID_CHARS, has_more=False, started=False)
            for _ in range(MAX_ENDPOINTS)),
        seen=(b'\xff' * FINGERPRINT_BYTES,) * MAX_SEEN_FINGERPRINTS)


# DERIVED, not chosen: the exact wire length of the worst-case token above. It
# is both the mint guarantee in `encode` and the reject bound in
# `_authenticated_body` / the Pydantic `max_length`, so widening a field bound
# or the dedup window cannot leave the server minting tokens it then 422s.
MAX_PAGE_TOKEN_CHARS = len(_wire(_worst_case()))
