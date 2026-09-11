"""mitmproxy addon: spool each `/aweme/v1/aweme/post/` RESPONSE for the driver.

The genuine TikTok app in the local Waydroid container is the only client whose
signature the `api32-core-alisg` gateway accepts for the account post feed
(measured 2026-09-10: our local signer AND the paid RapidAPI signer both get
HTTP 200 with a 0-byte body and `tt_orcas_res: 1`; the app's own signature
works, and still worked replayed from plain `curl` 611 s later). Its traffic is
already decrypted by the mitmproxy this project runs for identity capture — so
this addon taps it and writes each response into a spool directory, one JSON
file per response, keyed by the request's `user_id`. `device/driver.py` opens a
profile by intent and reads the newest entry that visit produced.

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
from harvest_spool import (POST_FEED_PATH, USER_ID_PARAM,  # noqa: E402
                           SpoolEntry, default_spool_dir, user_id_from_url,
                           write_entry)

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
        if path_from_url(url) != self._path:
            return
        user_id = user_id_from_url(url)
        if not user_id:
            # Nothing to key on, so nothing a driver could ever find. Logged
            # because a post-feed request without a `user_id` would mean the
            # app's param set changed.
            logger.warning('post-feed response with no %s param, not spooled', USER_ID_PARAM)
            return
        entry = SpoolEntry.from_response(user_id=user_id, captured_at=time.time(),
                                         body=_body(flow.response), path=self._path)
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
