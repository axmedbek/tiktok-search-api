"""mitmproxy addon: log the real TikTok app's REQUEST PARAMS for the param diff.

Runs outside the app under `mitmdump`, alongside the SSL-unpinned emulator, the
same way `capture_identity_addon.py` does — mitmproxy is deliberately NOT a
project dependency (`.claude/rules/dependencies.md`), so nothing in
`requirements.txt` mentions it. Every request the app makes to a TikTok host on
a matching path is appended as one JSONL line: the path, the masked query
params, the method, and which header NAMES were present.

Why: `/aweme/v1/aweme/post/` and `/aweme/v1/user/profile/other/` are live
handlers that reject this client's param set with a bodyless HTTP 200 and no
feedback. Headers, encoding, host/region and the web route are already ruled
out, so the missing ingredient is a query param, and this capture is the one
bounded way to learn its name. All the logic lives in
`tiktoksearch/capture_record.py`, which imports no mitmproxy and is
unit-testable; this file is only the hook.

Usage:
    # from the `mobile/` directory. THIS ADDON NEEDS NO PROJECT DEPENDENCY —
    # like `capture_identity_addon.py`, and unlike the diff CLI below. It
    # imports only `capture_record`, which is stdlib-only, so ANY mitmproxy
    # runs it: apt, pipx, or the venv. `mitmproxy` is deliberately NOT in
    # requirements.txt.
    #
    # It used to import `tiktoksearch.capture_diff`, which reaches `.client` ->
    # `.signing` -> `gmssl`. Under the apt mitmproxy that raised
    # `No module named 'gmssl'`: the proxy still came up, the addon SILENTLY
    # DID NOT LOAD, and a whole capture session would have recorded nothing. Do
    # not import anything else from the package here.
    mitmdump -s capture_requests_addon.py \
        --set capture_out=captured_requests.jsonl \
        --set capture_paths=/aweme/v1/

    # then, after opening a profile in the app and scrolling the post grid.
    # The DIFF side is the half that needs the venv: `capture_diff` imports
    # `.client` to build the expectation from the client's own param builders.
    ../.venv/bin/python -m tiktoksearch.capture_diff captured_requests.jsonl

    # `capture_diff` reads the warm identity's `device_query` KEY names (never a
    # value) to tell a param we already replay from a real gap. Point it at the
    # file explicitly when it is not where the server would look:
    ../.venv/bin/python -m tiktoksearch.capture_diff captured_requests.jsonl \
        --identities identities.json

APPEND-ONLY, and that difference from `capture_identity_addon.py` is
deliberate — do not "fix" one to match the other. That addon maintains a STATE
file (`identities.json`) which the running server hot-reloads on mtime, so it
rewrites atomically and only when the credentials actually changed, to avoid
mtime churn. This addon maintains a LOG: every request is evidence, nothing is
deduped, and nothing watches the file.

The output file carries device fingerprints even masked, so it is git-ignored.
Values of device-identifying and credential-shaped params are masked on the way
in (`capture_record.mask_value`); param and header NAMES are kept, because a name
is not a secret and the diff is about which names are present. Header VALUES
are never written — they hold the signature and the credentials.
"""
from __future__ import annotations

import logging
import os
import sys

from mitmproxy import ctx, http

# The addon is loaded as a top-level script by mitmdump, so the module below is
# not importable on its own — same insert `api_signed.py` does, and for the same
# reason. It must precede the import.
#
# The PACKAGE DIRECTORY goes on the path, not `mobile/`, so `capture_record` is
# imported as a top-level module and `tiktoksearch/__init__.py` never runs:
# that file imports `.client` eagerly, which is the `gmssl` chain this split
# exists to keep out of the addon. The diff CLI imports the same file the
# ordinary way, as `tiktoksearch.capture_record`.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoksearch'))

from capture_record import (DEFAULT_CAPTURE_OUT,  # noqa: E402
                            DEFAULT_CAPTURE_PATH_PREFIX, CapturedRequest,
                            is_tiktok_host, parse_path_prefixes, path_from_url,
                            path_matches)

logger = logging.getLogger('capture_requests')


class RequestCapture:
    def __init__(self) -> None:
        self._out = DEFAULT_CAPTURE_OUT
        self._prefixes: tuple[str, ...] = (DEFAULT_CAPTURE_PATH_PREFIX,)
        self._written = 0

    def load(self, loader) -> None:
        loader.add_option('capture_out', str, DEFAULT_CAPTURE_OUT,
                          'JSONL file to APPEND captured requests to')
        loader.add_option('capture_paths', str, DEFAULT_CAPTURE_PATH_PREFIX,
                          'Comma-separated path prefixes to capture (default: all /aweme/v1/*)')

    def configure(self, updated) -> None:
        self._out = ctx.options.capture_out or DEFAULT_CAPTURE_OUT
        self._prefixes = parse_path_prefixes(ctx.options.capture_paths or '')
        logger.info('appending captured requests to %s (paths: %s)',
                    self._out, ', '.join(self._prefixes))

    def request(self, flow: http.HTTPFlow) -> None:
        if not is_tiktok_host(flow.request.pretty_host or ''):
            return
        url = flow.request.pretty_url
        if not path_matches(path_from_url(url), self._prefixes):
            return
        record = CapturedRequest.from_request(url=url, method=flow.request.method,
                                              host=flow.request.pretty_host or '',
                                              header_names=flow.request.headers.keys())
        try:
            with open(self._out, 'a', encoding='utf-8') as handle:
                handle.write(record.as_json_line() + '\n')
        except OSError as exc:
            logger.error('failed to append to %s: %s', self._out, exc)
            return
        self._written += 1
        logger.info('captured #%d %s %s (%d params, %d headers)', self._written,
                    record.method, record.path, len(record.params), len(record.headers))


addons = [RequestCapture()]
