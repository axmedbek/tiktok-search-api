"""mitmproxy addon: harvest the live warm identity from the real TikTok app.

Run alongside the SSL-unpinned emulator (see memory: emulator-traffic-setup-state,
oracle-api-working). As the logged-in app talks to TikTok, this addon extracts the
current `x-tt-token`, the `sessionid` cookie, device_id/iid, and the full device
query fingerprint, then writes them to `identities.json`. The running search API
watches that file's mtime and hot-reloads (identity_manager.IdentityStore), so an
expired identity is replaced WITHOUT a restart — this is the auto-refresh loop that
fixes the "100/100 empty (hit_shark)" problem.

Usage:
    mitmdump -s capture_identity_addon.py \
        --set identities_out=identities.json \
        --set base_device_query=warm_device_query.json

`base_device_query` (optional) is a JSON file with the static half of the warm
fingerprint (cdid, openudid, region, mcc_mnc, ...) captured once; the addon merges
the live per-request bits (device_id, iid, x-tt-token, cookie) on top and only
rewrites the file when the identity actually changed.

`dyn_pair` (optional) is a JSON file holding the captured argus pair
`{"dyn_seed": "<f24>", "dyn_rand": <f3>}`, which the user-scoped endpoints
(`/aweme/v1/aweme/post/`) require and the search path does not. It is supplied
as a FILE rather than read off the wire because the pair lives inside the
encrypted `x-argus` header: recovering it means decrypting that header with the
sign key, which needs pycryptodome + gmssl, and this addon is deliberately
stdlib-only at import time so mitmdump can load it in any environment. The
addon's job is to carry the pair into `identities.json` beside the cookie it
belongs with, and to rewrite the file when either changes.

The pair is the SAME CLASS OF SECRET as the cookie: it is never logged and
never echoed. An entry carrying one half without the other is rejected by
`identity_manager`, so this addon writes both or neither.
"""
from __future__ import annotations

import json
import logging
import os
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from mitmproxy import ctx, http

logger = logging.getLogger('capture_identity')

# Only these hosts carry the app's authenticated API traffic.
_TIKTOK_HOST_MARKERS = ('tiktokv.com', 'tiktok.com', 'byteoversea.com', 'musical.ly')


class IdentityCapture:
    def __init__(self) -> None:
        self._out = 'identities.json'
        self._base_query: dict = {}
        self._dyn_pair: dict = {}
        self._last_written: dict | None = None

    def load(self, loader) -> None:
        loader.add_option('identities_out', str, 'identities.json',
                          'Path to write the captured identities.json')
        loader.add_option('base_device_query', str, '',
                          'Optional JSON file with the static warm device_query fingerprint')
        loader.add_option('dyn_pair', str, '',
                          'Optional JSON file with the captured argus pair {dyn_seed, dyn_rand}')

    def configure(self, updated) -> None:
        self._out = ctx.options.identities_out or 'identities.json'
        path = ctx.options.base_device_query
        if path and os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    self._base_query = json.load(f) or {}
                logger.info('loaded base device_query (%d keys) from %s', len(self._base_query), path)
            except (OSError, ValueError) as exc:
                logger.error('failed to read base_device_query %s: %s', path, exc)
        self._dyn_pair = self._read_dyn_pair(ctx.options.dyn_pair)
        # seed _last_written from an existing file so we don't rewrite on startup
        if os.path.exists(self._out):
            try:
                with open(self._out, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list) and data:
                    self._last_written = data[0]
            except (OSError, ValueError):
                pass

    def _read_dyn_pair(self, path: str) -> dict:
        """The captured argus pair from `path`, or {} when not configured.

        Both halves or neither: a file naming only one is a configuration
        error and is refused here rather than written into identities.json,
        where `identity_manager` would drop the whole identity. Nothing from
        the file reaches the log — only the field names and the path do.
        """
        if not path:
            return {}
        if not os.path.exists(path):
            logger.error('dyn_pair file %s not found — identities are written without the argus pair', path)
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
            seed = data.get('dyn_seed') or None
            rand = data.get('dyn_rand')
            rand = int(rand) if rand not in (None, '') else None
        except (OSError, ValueError, TypeError) as exc:
            logger.error('failed to read dyn_pair %s: %s', path, type(exc).__name__)
            return {}
        if bool(seed) != (rand is not None):
            logger.error('dyn_pair %s carries only one of dyn_seed/dyn_rand — the argus pair is one unit, so neither is written', path)
            return {}
        if seed is None:
            return {}
        logger.info('loaded the captured argus pair from %s', path)
        return {'dyn_seed': seed, 'dyn_rand': rand}

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host or ''
        if not any(m in host for m in _TIKTOK_HOST_MARKERS):
            return
        headers = flow.request.headers
        token = headers.get('x-tt-token') or headers.get('X-Tt-Token')
        cookie_hdr = headers.get('cookie') or headers.get('Cookie')
        if not (token or _has_sessionid(cookie_hdr)):
            return  # not an authenticated request

        qs = parse_qs(urlparse(flow.request.pretty_url).query)

        def q(name: str) -> str | None:
            v = qs.get(name)
            return v[0] if v else None

        device_id = q('device_id')
        iid = q('iid')
        if not device_id:
            return  # can't key an identity without a device_id

        # Build the device_query: static base + the live query params we saw.
        device_query = dict(self._base_query)
        for k, v in qs.items():
            if v:
                device_query.setdefault(k, v[0])
        device_query['device_id'] = device_id
        if iid:
            device_query['iid'] = iid

        identity = {
            'device_id': device_id,
            'iid': iid or device_query.get('iid', ''),
            'cookie': cookie_hdr or None,
            'x_tt_token': token or None,
            'user_agent': headers.get('user-agent') or headers.get('User-Agent'),
            'device_query': device_query,
        }
        # Both keys or neither — never one (identity_manager drops a half pair).
        identity.update(self._dyn_pair)
        self._maybe_write(identity)

    def _maybe_write(self, identity: dict) -> None:
        # Only rewrite when the credentials that actually matter changed —
        # otherwise every request would bump the mtime and force needless reloads.
        if self._last_written is not None:
            same = (self._last_written.get('device_id') == identity['device_id']
                    and self._last_written.get('cookie') == identity['cookie']
                    and self._last_written.get('x_tt_token') == identity['x_tt_token']
                    # A re-captured argus pair is a credential change like any
                    # other: without this the file keeps the stale pair until
                    # the cookie happens to rotate.
                    and self._last_written.get('dyn_seed') == identity.get('dyn_seed')
                    and self._last_written.get('dyn_rand') == identity.get('dyn_rand'))
            if same:
                return
        tmp = self._out + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump([identity], f, indent=2)
            os.replace(tmp, self._out)  # atomic — the watcher never sees a half file
        except OSError as exc:
            logger.error('failed to write %s: %s', self._out, exc)
            return
        self._last_written = identity
        # Secrets are masked or reduced to a yes/no; the argus pair is reported
        # only as present/absent, never as a value, not even masked.
        logger.info('captured fresh identity device_id=%s token=%s cookie_sessionid=%s argus_pair=%s -> %s',
                    identity['device_id'],
                    _mask(identity['x_tt_token']),
                    'yes' if _has_sessionid(identity['cookie']) else 'no',
                    'yes' if identity.get('dyn_seed') else 'no',
                    self._out)


def _has_sessionid(cookie_hdr: str | None) -> bool:
    if not cookie_hdr:
        return False
    try:
        jar = SimpleCookie()
        jar.load(cookie_hdr)
        return 'sessionid' in jar and bool(jar['sessionid'].value)
    except Exception:
        return 'sessionid=' in cookie_hdr


def _mask(v: str | None) -> str:
    if not v:
        return '(none)'
    return v[:6] + '…' + v[-4:] if len(v) > 12 else '***'


addons = [IdentityCapture()]
