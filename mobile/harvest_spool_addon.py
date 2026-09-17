"""mitmproxy addon: spool each `/aweme/v1/aweme/post/` RESPONSE for the driver.

The genuine TikTok app in the local Waydroid container is the only client whose
signature the `api32-core-alisg` gateway accepts for the account post feed
(measured 2026-09-10: our local signer AND the paid RapidAPI signer both get
HTTP 200 with a 0-byte body and `tt_orcas_res: 1`; the app's own signature
works, and still worked replayed from plain `curl` 611 s later). Its traffic is
already decrypted by the mitmproxy this project runs for identity capture — so
this addon taps it and writes each response into a spool directory, one JSON
file per response, keyed by the account uid (the request's `user_id` param, or
the body's `author.uid` when the request carries none). `device/driver.py`
opens a profile by intent and merges the entries that visit produced.

    # from the `mobile/` directory, ALONGSIDE the existing capture. `-w` and
    # every other `-s` keep working: this addon only READS flow objects — it
    # never rewrites a request, a response or a body — so the flow file records
    # exactly what it recorded before.
    mitmdump -w flows.mitm \
        -s capture_identity_addon.py \
        -s capture_requests_addon.py \
        -s harvest_spool_addon.py \
        --set harvest_spool_dir=harvest_spool

    # then run the worker against the same directory:
    .venv/bin/python mobile/worker.py --source device --spool-dir mobile/harvest_spool

**STDLIB-ONLY AT IMPORT TIME, and that is not a style preference.** `mitmdump`
runs on the distro/pipx interpreter, not the project `.venv`.
`capture_requests_addon.py` records what happens otherwise: it imported
`tiktoksearch.capture_diff`, which reaches `.client` -> `.signing` -> `gmssl`,
mitmdump answered `No module named 'gmssl'`, the proxy came up looking healthy,
the addon SILENTLY DID NOT LOAD, and a whole session would have recorded
nothing. So this file follows the same split: the PACKAGE DIRECTORY goes on
`sys.path` and the two stdlib-only modules are imported as TOP-LEVEL modules,
never through `tiktoksearch/__init__.py`, which imports `.client` eagerly. Do
not import anything else from the package here.

The spool holds public post data — captions, ids, counters — and no credential:
request headers are never read, and no cookie, `x_tt_token` or auth header is
written or logged. `mobile/identities.json` is neither read nor written by this
addon. The directory is git-ignored all the same, because a feed is bulk data
nobody wants in a diff.
"""
from __future__ import annotations

import logging
import os
import sys
import time

from mitmproxy import ctx, http

# The addon is loaded as a top-level script by mitmdump, so the modules below
# are not importable on their own — the same `sys.path` insert
# `capture_requests_addon.py` and `api_signed.py` do, and for the same reason.
# It must precede the imports.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tiktoksearch'))

from capture_record import is_tiktok_host, path_from_url  # noqa: E402
from harvest_spool import (POST_FEED_PATH, PROFILE_PATH, STATUS_OK, UNIQUE_ID_PATH, USER_ID_PARAM,  # noqa: E402
                           ProfileEntry, default_spool_dir, entry_for_response,
                           write_entry, write_profile_entry)

logger = logging.getLogger('harvest_spool')


class HarvestSpool:
    """Writes one spool entry per post-feed response."""

    def __init__(self) -> None:
        self._dir = default_spool_dir()
        self._path = POST_FEED_PATH
        self._written = 0

    def load(self, loader) -> None:
        # Option names prefixed `harvest_spool_`, so this addon can be loaded
        # in the SAME mitmdump as `capture_requests_addon.py` (`capture_out`,
        # `capture_paths`) without either shadowing the other's option.
        loader.add_option('harvest_spool_dir', str, default_spool_dir(),
                          'Directory to write post-feed spool entries into')
        loader.add_option('harvest_spool_path', str, POST_FEED_PATH,
                          'Exact request path to spool (default: the account post feed)')

    def configure(self, updated) -> None:
        self._dir = ctx.options.harvest_spool_dir or default_spool_dir()
        self._path = ctx.options.harvest_spool_path or POST_FEED_PATH
        logger.info('spooling %s responses to %s', self._path, self._dir)

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None:
            return
        if not is_tiktok_host(flow.request.pretty_host or ''):
            return
        url = flow.request.pretty_url
        # EXACT path, not a prefix: `capture_requests_addon.py` captures the
        # whole `/aweme/v1/` family for the param diff, and this spool is one
        # endpoint's response bodies. A prefix here would fill the directory
        # with feeds the driver never reads.
        path = path_from_url(url)
        if path == UNIQUE_ID_PATH:
            self._spool_resolver(url, flow.response)
            return
        if path == PROFILE_PATH:
            self._spool_profile(url, flow.response)
            return
        if path != self._path:
            return
        # Keyed by the URL's `user_id` when present, else by the account uid
        # the body names — the 46.9.3 app sends `sec_user_id` and no `user_id`
        # (measured 2026-09-14), so the body is the usual source.
        entry = entry_for_response(url=url, captured_at=time.time(), body=_body(flow.response),
                                   path=self._path)
        if not entry.user_id:
            # Nothing to key on, so nothing a driver could ever find. Counts
            # and flags only — never the body.
            # The body of a SMALL unreadable reply is TikTok's own error text
            # (measured: `status_msg`, no user data) and is the only thing that
            # tells a private account from a risk-control refusal, so its head
            # is logged; a large body is never logged.
            snippet = _snippet(flow.response) if entry.status != STATUS_OK and entry.byte_length <= MAX_SNIPPET_BYTES else ''
            logger.warning('post-feed response with no %s param and no account uid in the body, '
                           'not spooled (status=%s aweme_list=%d bytes=%d)%s',
                           USER_ID_PARAM, entry.status, len(entry.aweme_list), entry.byte_length, snippet)
            return
        user_id = entry.user_id
        try:
            target = write_entry(self._dir, entry)
        except (OSError, ValueError) as exc:
            logger.error('failed to spool a %s response: %s', self._path, exc)
            return
        self._written += 1
        # Counts and flags only — never the body, never a caption.
        logger.info('spooled #%d %s: user_id=%s status=%s aweme_list=%d has_more=%s bytes=%d',
                    self._written, os.path.basename(target), user_id, entry.status,
                    len(entry.aweme_list), entry.has_more, entry.byte_length)


    def _spool_resolver(self, url: str, response: http.Response) -> None:
        entry = ProfileEntry.from_resolver_response(url=url, captured_at=time.time(), body=_body(response))
        # Successful resolution carries no full profile; its later profile
        # response must remain the source of account fields and feed ownership.
        if entry is None:
            raw = _body(response) or b''
            if not raw.startswith(b'{'):
                # Shape only: a non-JSON resolver reply is how throttling or a
                # format change would first show up.
                logger.warning('resolver reply not JSON: http=%s bytes=%d orcas=%s ctype=%s%s', response.status_code,
                               len(raw), response.headers.get('tt_orcas_res'), response.headers.get('content-type'),
                               _snippet(response) if len(raw) <= MAX_SNIPPET_BYTES else '')
            return
        self._write_profile(entry, path=UNIQUE_ID_PATH)

    def _spool_profile(self, url: str, response: http.Response) -> None:
        # The handle -> uid resolve the driver waits for. Keyed by the reply's
        # own username, else by the request's `sec_user_id` for an error reply.
        entry = ProfileEntry.from_response(url=url, captured_at=time.time(), body=_body(response))
        if entry.status != STATUS_OK:
            # Shape only, plus the head of a SMALL body: an unreadable profile
            # reply is how a cold / distrusted device shows up (0 bytes with
            # `tt_orcas_res: 1`), and that is a different fault from a parser gap.
            raw = _body(response) or b''
            logger.warning('profile reply unreadable: http=%s bytes=%d orcas=%s ctype=%s%s',
                           response.status_code, len(raw), response.headers.get('tt_orcas_res'),
                           response.headers.get('content-type'),
                           _snippet(response) if len(raw) <= MAX_SNIPPET_BYTES else '')
        self._write_profile(entry, path=PROFILE_PATH)

    def _write_profile(self, entry: ProfileEntry, *, path: str) -> None:
        if not entry.handle:
            logger.warning('%s response with no account key, not spooled (status_code=%s)', path, entry.status_code)
            return
        try:
            target = write_profile_entry(self._dir, entry)
        except (OSError, ValueError) as exc:
            logger.error('failed to spool a %s response: %s', path, exc)
            return
        # Counts and flags only.
        logger.info('spooled profile %s: status=%s status_code=%s user_id=%s aweme_count=%s',
                    os.path.basename(target), entry.status, entry.status_code, entry.user_id, entry.aweme_count)


MAX_SNIPPET_BYTES = 600


def _snippet(response: http.Response) -> str:
    """` body=<head of the body>` for a small error reply, or '' when unreadable."""
    try:
        raw = response.content or b''
    except ValueError:
        return ''
    return ' body=' + raw[:MAX_SNIPPET_BYTES].decode('utf-8', errors='replace')


def _body(response: http.Response) -> bytes | None:
    """The decoded response body, or None when it cannot be decoded.

    Responses are gzip-encoded upstream and mitmproxy decodes them on
    `.content`; a truncated or unknown encoding makes that raise instead. None
    is passed on as `status: unreadable` with an explicit reason, which is a
    RESPONSE THAT ARRIVED — the one thing the driver must be able to tell apart
    from "no response yet"."""
    try:
        return response.content or b''
    except ValueError:
        logger.warning('response body could not be decoded, spooling it as unreadable')
        return None


addons = [HarvestSpool()]
