"""The capture RECORD: what the mitmproxy addon writes, and how it is read back.

Deliberately STDLIB-ONLY, and that is the whole reason this module exists apart
from `capture_diff.py`. `capture_requests_addon.py` runs under `mitmdump`,
which is normally the distro/pipx mitmproxy on an interpreter that is NOT the
project `.venv`, so it can import no project dependency at all. Importing
`capture_diff` from the addon pulled `.client` -> `.signing` -> `tiktok_signer`
-> `gmssl`, and mitmdump answered `No module named 'gmssl'`: the proxy came up,
the addon did NOT load, and a whole capture session would have recorded nothing
while looking healthy. So nothing here imports from this package or from
outside the standard library, and the test of that is, on a bare system
interpreter:

    PYTHONPATH=mobile/tiktoksearch python3 -c "import capture_record"

Top-level, NOT as `tiktoksearch.capture_record`: the package `__init__.py`
imports `.client` eagerly, so going through the package drags `gmssl` back in
however clean this file is. That is why the addon puts the package DIRECTORY on
`sys.path` — this module is the one file in here that must stay importable on
its own. `capture_diff` imports it the ordinary relative way, from the venv,
where the package import costs nothing.

The split is by dependency, not by convenience: matching the host, matching the
path prefix, parsing the query string, masking the sensitive values and
appending one JSONL line need no client. Only `capture_diff.expected_params`
does — it builds its expectation with `client.py`'s own builders — and that
runs in the CLI, in the venv, after the capture session is over.

Values are masked, names never are. A capture file holding a live `device_id`
is a burned identity (`.claude/rules/security.md`), and the diff this feeds is
about which NAMES are present anyway.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit

logger = logging.getLogger('tiktoksearch.capture_record')

# Hosts that carry the app's API traffic. `capture_identity_addon.py` holds the
# same list privately; the two addons are independent scripts and that one is
# not importable from here (it imports mitmproxy at module level).
TIKTOK_HOST_MARKERS: tuple[str, ...] = ('tiktokv.com', 'tiktok.com', 'byteoversea.com', 'musical.ly')
# Defaults shared with `capture_requests_addon.py`, which is only a hook.
DEFAULT_CAPTURE_OUT = 'captured_requests.jsonl'
DEFAULT_CAPTURE_PATH_PREFIX = '/aweme/v1/'
PATH_PREFIX_SEP = ','

# --- Masking ----------------------------------------------------------------
# Params whose VALUE identifies this device. A leaked `device_id` in a
# committed capture file is a burned identity, so these are masked even though
# they are not credentials in the cookie/token sense.
#
# The second row is the ALIAS row, and it is the reason this list is not just
# four names: the app sends the same identifier under more than one name in the
# same query string — `install_id` is byte-identical to `iid`, `android_id` to
# `openudid` — so masking only one spelling publishes the value under the
# other. `google_aid`/`gaid`/`oaid`/`mac_address`/`imei` are hardware and
# advertising ids that match no `SENSITIVE_NAME_MARKERS` substring either.
# Masking is applied on the way into the capture file AND on the way out to
# stdout, so an unmasked spelling leaks to terminal scrollback and to any
# pasted report, not merely to a git-ignored file.
SENSITIVE_PARAM_NAMES: frozenset[str] = frozenset((
    'device_id', 'iid', 'openudid', 'cdid',
    'install_id', 'android_id', 'google_aid', 'gaid', 'oaid', 'mac_address', 'imei',
))
# Substrings that mark a name as credential- or fingerprint-shaped whatever the
# app calls it. `sec_user_id` / `sec_uid` deliberately match NOTHING here: a
# sec_uid is a public, opaque identifier that appears in share URLs and grants
# no access (architecture.md § data flow invariants), and `keyword` is the
# caller's own search term, not a secret.
#
# The second row is TikTok's own persistent-identifier vocabulary, and not one
# of these names matches a substring in the first row: `webid`/`web_id` is the
# long-lived web device id, `odin_tt` and `uid_tt` are tracking/user cookies
# the app also sends as query params on some routes, and `ttreq` is the
# per-request tracker. None of them is in today's `device_query`, so this is
# not a live leak — but they DO occur in TikTok cookies and query strings, and
# a capture file holding any of them is a burned identity, which is the whole
# reason this list errs wide. `webid` covers `webid_last_time` and friends too.
SENSITIVE_NAME_MARKERS: tuple[str, ...] = (
    'token', 'cookie', 'session', 'sid', 'secret', 'passport', 'udid', 'uuid', 'serial',
    'webid', 'web_id', 'odin_tt', 'uid_tt', 'ttreq',
)
# Same shape as `capture_identity_addon._mask`: keep enough to recognise a
# value across two captures, never enough to replay it.
MASK_KEEP_HEAD = 6
MASK_KEEP_TAIL = 4
MASK_MIN_LEN = 12
MASK_ELLIPSIS = '…'
MASK_SHORT = '***'
MASK_ABSENT = '(none)'
# The exact rendered length of a masked value. Idempotence is decided on this
# full SHAPE and never on "contains an ellipsis" — see `is_masked_value`.
MASK_SHAPE_LEN = MASK_KEEP_HEAD + len(MASK_ELLIPSIS) + MASK_KEEP_TAIL

# --- Capture record schema (written by the addon, read by the CLI) ----------
FIELD_PATH = 'path'
FIELD_METHOD = 'method'
FIELD_HOST = 'host'
FIELD_PARAMS = 'params'
FIELD_HEADERS = 'headers'


def is_tiktok_host(host: str) -> bool:
    """Whether `host` is one of the app's API hosts."""
    return any(marker in host for marker in TIKTOK_HOST_MARKERS)


def parse_path_prefixes(raw: str) -> tuple[str, ...]:
    """The addon's `capture_paths` option as a tuple of path prefixes. An empty
    value means the default `/aweme/v1/` prefix, never "capture everything":
    this file is committed-adjacent and every extra request is more fingerprint
    on disk."""
    prefixes = tuple(part.strip() for part in raw.split(PATH_PREFIX_SEP) if part.strip())
    return prefixes or (DEFAULT_CAPTURE_PATH_PREFIX,)


def path_matches(path: str, prefixes: Iterable[str]) -> bool:
    """Whether `path` starts with any of `prefixes`."""
    return any(path.startswith(prefix) for prefix in prefixes)


def path_from_url(url: str) -> str:
    """The path of `url`, without host or query string."""
    return urlsplit(url).path


def params_from_url(url: str) -> dict[str, str]:
    """The query string of `url` as a flat name -> value dict.

    First value wins on a repeated name — the convention
    `capture_identity_addon.py` already uses (`parse_qs` then `v[0]`). Blank
    values are kept: `foo=` present is a different request from `foo` absent,
    and presence is what this tool measures."""
    params: dict[str, str] = {}
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        params.setdefault(name, value)
    return params


def is_sensitive_param(name: str) -> bool:
    """Whether this param's VALUE must be masked before it is written down."""
    lowered = name.lower()
    if lowered in SENSITIVE_PARAM_NAMES:
        return True
    return any(marker in lowered for marker in SENSITIVE_NAME_MARKERS)


def is_masked_value(value: str) -> bool:
    """Whether `value` is ALREADY the output of `mask_value`, matched on the
    full masked shape.

    Why a guard exists at all: masking runs on the way into the capture file
    AND again on the way out (a hand-edited or third-party file is external
    data), so an already-masked value has to survive the second pass unchanged.
    `681234…4567` is 11 characters, which trips the short-value rule and would
    otherwise degrade to `***` — throwing away exactly the recognisability the
    shape exists for.

    Why the shape and not mere containment: `parse_qsl` percent-DECODES, so an
    upstream value carrying `%E2%80%A6` arrives holding a literal `…`. A
    containment test then waves that whole raw identifier straight through to
    the capture file and to stdout — masking bypassed by an attacker-chosen
    substring, on the one path whose entire job is to prevent that. Requiring
    the full shape bounds the pass-through to values already indistinguishable
    from a mask: exactly `MASK_SHAPE_LEN` chars with the ellipsis at exactly
    the head offset (and such a value is short enough that masking it would
    have produced `MASK_SHORT`, i.e. it carries no more than the head+tail a
    mask publishes anyway).

    The head and tail are deliberately NOT checked for a further ellipsis: a
    genuine mask output can hold one when the raw value did (`abc…de` + `…` +
    `7890`), and rejecting that would reintroduce the `***` degradation."""
    return (len(value) == MASK_SHAPE_LEN
            and value[MASK_KEEP_HEAD:MASK_KEEP_HEAD + len(MASK_ELLIPSIS)] == MASK_ELLIPSIS)


def mask_value(name: str, value: str | None) -> str:
    """`value` as it may be written to disk or printed — masked when `name` is
    sensitive, verbatim otherwise.

    The NAME is always kept, whatever it is: a param name is not a secret, and
    it is the entire signal this tool exists to compare."""
    if not is_sensitive_param(name):
        return value if value is not None else MASK_ABSENT
    if not value:
        return MASK_ABSENT
    if is_masked_value(value):
        return value
    if len(value) <= MASK_MIN_LEN:
        return MASK_SHORT
    return value[:MASK_KEEP_HEAD] + MASK_ELLIPSIS + value[-MASK_KEEP_TAIL:]


def mask_params(params: Mapping[str, str]) -> dict[str, str]:
    """Every value masked by its own name's rule."""
    return {name: mask_value(name, value) for name, value in params.items()}


@dataclass(frozen=True, slots=True)
class CapturedRequest:
    """One request the app made, as one JSONL line.

    `params` values are ALREADY masked — the masking happens on the way in, so
    an unmasked value never reaches the file. `headers` carries header NAMES
    only, never values: a header diff is a name question too, and the values
    are the signature and the credentials."""
    path: str
    method: str
    host: str
    params: Mapping[str, str]
    headers: tuple[str, ...]

    @classmethod
    def from_request(cls, *, url: str, method: str, host: str,
                     header_names: Iterable[str]) -> 'CapturedRequest':
        """A record for one live request. Masks as it builds."""
        return cls(path=path_from_url(url), method=method.upper(), host=host,
                   params=mask_params(params_from_url(url)),
                   headers=_unique_header_names(header_names))

    @classmethod
    def from_mapping(cls, data: object) -> 'CapturedRequest':
        """One parsed JSONL line, validated at this boundary. Raises
        `ValueError` on anything unusable so the loader can skip that line
        loudly instead of carrying a half record into the diff."""
        if not isinstance(data, Mapping):
            raise ValueError(f'expected a JSON object, got {type(data).__name__}')
        path = data.get(FIELD_PATH)
        if not isinstance(path, str) or not path:
            raise ValueError(f'{FIELD_PATH!r} missing or not a non-empty string')
        raw_params = data.get(FIELD_PARAMS) or {}
        if not isinstance(raw_params, Mapping):
            raise ValueError(f'{FIELD_PARAMS!r} is not a JSON object')
        raw_headers = data.get(FIELD_HEADERS) or ()
        if not isinstance(raw_headers, (list, tuple)):
            raise ValueError(f'{FIELD_HEADERS!r} is not a JSON array')
        # Masked on the way in, and masked again here: a hand-edited or
        # third-party capture file is external data like any other.
        params = mask_params({str(k): str(v) for k, v in raw_params.items()})
        return cls(path=path, method=str(data.get(FIELD_METHOD) or ''),
                   host=str(data.get(FIELD_HOST) or ''), params=params,
                   headers=_unique_header_names(str(h) for h in raw_headers))

    def as_json_line(self) -> str:
        """This record as one JSONL line, without its newline."""
        return json.dumps({FIELD_PATH: self.path, FIELD_METHOD: self.method,
                           FIELD_HOST: self.host, FIELD_PARAMS: dict(self.params),
                           FIELD_HEADERS: list(self.headers)}, ensure_ascii=False)


def _unique_header_names(names: Iterable[str]) -> tuple[str, ...]:
    """Header names, lower-cased, deduped, in the order the app sent them."""
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name.lower(), None)
    return tuple(seen)


def load_capture(path: str) -> list[CapturedRequest]:
    """Every usable record in a JSONL capture file, in file order.

    An unusable line is logged and skipped rather than failing the run: a
    capture session is expensive to repeat, and one truncated last line (the
    addon appends live, so the file may be read mid-write) must not throw the
    rest away. The log names the line NUMBER, never its content."""
    records: list[CapturedRequest] = []
    with open(path, 'r', encoding='utf-8') as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(CapturedRequest.from_mapping(json.loads(line)))
            except (ValueError, TypeError) as exc:
                logger.warning('skipping unusable capture line %d: %s', lineno, exc)
    return records
