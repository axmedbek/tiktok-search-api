"""Vendored pure-Python v46 signer.

`MetasecSigner` wraps the vendored `Metasec` implementation. Fed the v46
`sign_*` params (see `v46_params`) plus a warm identity, it produces headers
TikTok's v46 search backend accepts — measured on the direct path with zero
RapidAPI calls. It is signature-compatible with `RapidSigner`, so `client.py`
can hold either one.

Two constructions, and the difference is structural, not a flag the caller
passes: `for_v46` is `signer: local` (v46 params, warm identity, v46 headers),
while plain `MetasecSigner(config)` is the cold `legacy` path — v32 defaults and
NO identity, whatever the config happens to carry. See `sign`.

The standing risk: `metasec.DEFAULT_SIGN_KEY` and `GORGON_TABLE` are static
constants extracted from one `libmetasec_ov.so` build. They hold while
`mssdk_ver_code` does not move; when TikTok bumps the MSSDK someone must
re-extract the key. That failure is SILENT — the signer keeps producing a
well-formed signature that risk-control answers with an empty `data[]` — so the
RapidAPI fallback cannot catch it; RapidAPI is the diagnostic for it (flip one
profile to `signer: rapid` and re-run the query) and the cover for hard signing
failures.
"""
from __future__ import annotations
import logging
import random
import struct
import time
import zlib
from dataclasses import replace
from urllib.parse import urlsplit
from .config import ClientConfig
from .errors import TransportError
# `MetasecBaseException` and `ProtoError` MUST come from the package, not from
# `.tiktok_signer.exception` / `.tiktok_signer.protobuf.protobuf`: the vendored
# modules import each other flatly (`from exception import ...`,
# `from protobuf.protobuf import ProtoBuf`) through the sys.path shim in
# `tiktok_signer/__init__`, so importing the same file by its package path
# yields a SECOND module object with DIFFERENT class objects, and `except` on
# those would never fire. The package re-exports are the shim-loaded ones, and
# it asserts that identity at import time.
from .tiktok_signer import Metasec, MetasecBaseException, ProtoError

logger = logging.getLogger('tiktoksearch.signing')
# Static header values a v46 mobile request carries alongside the signature.
# Same values RapidSigner sends — the two signers must be interchangeable at the
# call site, headers included, or swapping them would change the fingerprint.
SDK_VERSION_HEADER = '2'
BD_KMSV_HEADER = '0'
# "this device is logged in", sent whenever the identity carries a cookie.
DM_STATUS_LOGGED_IN = 'login=1;ct=1;rt=1'
# The quad `Metasec.sign` must return. Checked explicitly, as `RapidSigner` does
# for its provider: a reply missing one would otherwise be a raw `KeyError`,
# i.e. an HTTP 500 with no fallback and no 502, instead of a signing failure.
SIGNATURE_KEYS: tuple[str, ...] = ('x-argus', 'x-gorgon', 'x-ladon', 'x-khronos')
# What a signing call can really fail with, and therefore what may trigger the
# paid fallback. metasec's own errors (`InvalidURL` in practice); `ProtoError`,
# which the vendored protobuf encoder raises for a field it cannot type and is
# NOT a `MetasecBaseException` — reachable whenever a string-typed sign field is
# `None`, which `from_mapping` permits because it filters config by key and
# never by value (a bare `sign_app_version:` in YAML lands as `None`); and the
# ValueError / struct / zlib family the crypto helpers raise (`binascii.Error`
# and pycryptodome's pad/AES errors are `ValueError` subclasses).
# Deliberately NOT `Exception`: a programming error must not buy a signature —
# in particular a `TypeError` from a changed `Metasec.sign` signature.
_SIGNING_FAILURES: tuple[type[BaseException], ...] = (MetasecBaseException, ProtoError, ValueError, struct.error, zlib.error)
# Logged once per local-signer construction (so a hot-reloaded identity is
# covered too) when an identity arrived without its captured UA. Not fatal:
# `pool._build_slots` builds a synthetic, identity-free client when
# `identities.json` is absent, so raising here would turn a missing file into a
# startup crash. Device id only — never a cookie or token.
LOCAL_NO_UA_MSG = ('Local v46 signer has no captured user_agent (device=%s): the User-Agent falls back to a built one quoting version_code=%s while the signature says app_version=%s. TikTok reads that app/signer version mismatch as hit_shark and answers HTTP 200 with an empty data[], which surfaces as SoftError/502 and retires the identity. Re-capture this identity WITH its user_agent.')
DEVICE_QUERY_APP_VERSION_KEY = 'version_name'
# Logged once per local-signer construction when the configured
# `sign_app_version` disagrees with the app version the identity itself was
# captured under. This is anti-block invariant (c): a signature claiming a
# different app version than the device fingerprint is read as hit_shark, and
# it is otherwise silent — the signature is well-formed and the reply is a
# perfectly ordinary empty `data[]`. WARNING rather than a raise, for the same
# reason LOCAL_NO_UA_MSG is: a synthetic identity-free client must still boot.
# Carries two version strings and a device id — no cookie, token or pair.
LOCAL_APP_VERSION_MISMATCH_MSG = ('Local v46 signer will sign app_version=%s while the identity (device=%s) was captured under version_name=%s. TikTok reads a signer/device version mismatch as hit_shark and answers HTTP 200 with an empty data[]. Set sign_app_version to the identity\'s own app version.')

def v46_params(config: ClientConfig) -> ClientConfig:
    """`config` with its v46 `sign_*` values promoted into the four generic
    fields the signer actually reads.

    This mapping is the whole reason the vendored signer looked dead on v46:
    `app_version`, `sdk_version`, `sdk_version_code` and `license_id` are
    ARGUMENTS to `Metasec.sign`, not baked constants, and `ClientConfig`'s
    defaults for them are the v32 ones. Only `RapidSigner` ever read the
    `sign_*` values, so a v46 warm identity was being signed as a v32 app — a
    signer/device version mismatch, which is hit_shark by anti-block invariant
    (c), not a broken signer.

    It lives here, beside the only code that consumes those four fields, so no
    caller can build a v46 local signer and forget it. `dataclasses.replace`
    keeps the frozen config unmutated.
    """
    return replace(config, app_version=config.sign_app_version, sdk_version=config.sign_mssdk_ver_str, sdk_version_code=int(config.sign_mssdk_ver_code), license_id=int(config.sign_license_id))

class MetasecSigner:

    def __init__(self, config: ClientConfig, *, launch_time: int | None=None, dyn_pair_paths: frozenset[str]=frozenset()) -> None:
        self._config = config
        self._metasec = Metasec()
        self._launch_time = launch_time or int(time.time()) - random.randint(600, 7200)
        # The paths the captured argus pair — and therefore the corrected v46.9
        # payload — may be used on. INJECTED by `client.py` rather than named
        # here, for two reasons: `client` imports this module, so this module
        # cannot import `client`'s path constants back; and the set is the SAME
        # object that drives host routing (`client.USER_SCOPED_PATHS`, read by
        # `_host_for`), so a path cannot end up on the user-scoped gateway with
        # the legacy payload, or on the search gateway with the corrected one.
        # Empty by default: a signer nobody gave the set to signs every path
        # the way it always did.
        self._dyn_pair_paths = dyn_pair_paths
        # Only `for_v46` flips this. Gating on the CONSTRUCTION rather than on
        # `config.cookie` is what keeps `legacy` identity-free by construction:
        # a profile that resolves to legacy while carrying identities (an
        # operator commenting out `signer:`, or a config predating the knob)
        # must not send a live warm cookie under a v32 signature — that is
        # anti-block invariant (c) on a real identity.
        self._v46 = False

    @classmethod
    def for_v46(cls, config: ClientConfig, *, launch_time: int | None=None, dyn_pair_paths: frozenset[str]=frozenset()) -> 'MetasecSigner':
        """The signer for `signer: local` — same class, fed `v46_params`, and the
        only construction that attaches the warm identity and the v46 headers.
        Plain construction stays the v32 cold legacy path."""
        signer = cls(v46_params(config), launch_time=launch_time, dyn_pair_paths=dyn_pair_paths)
        signer._v46 = True
        if not config.user_agent:
            logger.warning(LOCAL_NO_UA_MSG, config.device_id or '<synthetic>', config.version_code, config.sign_app_version)
        # Read off the identity's OWN captured fingerprint, so the check is
        # against what the device claims upstream rather than against another
        # config value. Absent on a synthetic identity-free client, where there
        # is nothing to disagree with and the check is skipped.
        captured = (config.device_query or {}).get(DEVICE_QUERY_APP_VERSION_KEY)
        if captured and str(captured) != config.sign_app_version:
            logger.warning(LOCAL_APP_VERSION_MISMATCH_MSG, config.sign_app_version, config.device_id or '<synthetic>', captured)
        return signer

    def user_agent(self) -> str:
        """The identity's captured UA on the v46 path when there is one, else a
        UA built from the config. Same precedence as `RapidSigner.user_agent`.
        The captured UA is identity material, so `legacy` never uses it.

        A `local` profile is expected to carry the captured UA: the built
        fallback quotes `version_code`, which `v46_params` deliberately leaves
        alone (the probe that validated this path did the same), so it would not
        match the v46 app version in the signature — hence `LOCAL_NO_UA_MSG`."""
        cfg = self._config
        if self._v46 and cfg.user_agent:
            return cfg.user_agent
        return f'com.zhiliaoapp.musically/{cfg.version_code} (Linux; U; Android {cfg.os_version}; en; {cfg.device_type}; Build/RP1A.200720.012; Cronet/TTNetVersion:)'

    @staticmethod
    def _request_path(url: str) -> str:
        """The path component of a signed URL, for the per-path pair gate.

        Parsed rather than string-matched so a query value that happens to
        contain an endpoint path cannot select the corrected payload, and so a
        malformed URL degrades to `''` — which is in no path set, i.e. it signs
        the legacy way. `Metasec.sign` rejects a URL with no query string
        anyway, so this never has to decide anything a real request depends on
        beyond which payload it gets."""
        return urlsplit(url).path

    def _signature(self, url: str, device_id: str, payload: bytes | None) -> dict:
        """The raw x-argus/x-ladon/x-gorgon/x-khronos quad.

        A local signing failure is surfaced as `TransportError` — the same class
        `RapidSigner` raises when signing fails — so the client can treat "no
        headers were produced" identically whichever signer it holds, and so
        `client.py`'s narrow paid fallback has exactly one trigger.

        The handler is narrow (`_SIGNING_FAILURES`) because it is what decides
        whether the fallback spends money: it covers metasec's own
        `MetasecBaseException` tree, the protobuf encoder's `ProtoError`, and the
        `ValueError` / `struct.error` / `zlib.error` family raised by the crypto
        helpers, and nothing else — a `TypeError` from a changed `Metasec.sign`
        signature is a bug and must surface as one. Note the key-format
        `InvalidEncryptionKey` can never arrive here — it is raised in
        `Metasec.__init__`, i.e. in `MetasecSigner.__init__`, outside this call.
        Only the exception CLASS name is carried into the message: `InvalidURL`'s
        own message embeds the full signed query string, and `ProtoError`'s
        embeds the offending field's type."""
        cfg = self._config
        # TWO gates, and both must pass before the corrected payload is used.
        #
        # (1) The captured argus pair is IDENTITY material, so it rides on the
        #     `for_v46` construction only — the same rule the cookie, token and
        #     captured UA follow, and for the same reason: a `legacy` signer
        #     must keep signing exactly what it signed before the pair existed.
        # (2) PER PATH. The pair buys entry to the user-scoped gateway and
        #     nothing else. `/search` lives on a different gateway where the
        #     LEGACY payload is measured to work, and no measurement says the
        #     corrected one is accepted there — so an identity that happens to
        #     carry a pair must not silently re-sign the one path that already
        #     works. Anything off `_dyn_pair_paths` signs exactly as it did
        #     before the pair existed.
        use_pair = self._v46 and self._request_path(url) in self._dyn_pair_paths
        dyn_seed = cfg.dyn_seed if use_pair else None
        dyn_rand = cfg.dyn_rand if use_pair else None
        try:
            sig = self._metasec.sign(url=url, app_id=cfg.app_id, app_version=cfg.app_version, app_launch_time=self._launch_time, device_type=cfg.device_type, sdk_version=cfg.sdk_version, sdk_version_code=cfg.sdk_version_code, license_id=cfg.license_id, device_id=device_id, device_token='', dyn_seed=dyn_seed, dyn_version=cfg.dyn_version, rand=dyn_rand, payload=payload.hex() if isinstance(payload, (bytes, bytearray)) else payload)
        except _SIGNING_FAILURES as exc:
            raise TransportError(f'local signer failed to produce headers: {type(exc).__name__}') from exc
        missing = [k for k in SIGNATURE_KEYS if k not in sig]
        if missing:
            raise TransportError(f'local signer returned a reply missing {missing}')
        return sig

    def sign(self, *, url: str, device_id: str, payload: bytes | None=None, iid: str='') -> dict[str, str]:
        """Sign one request URL.

        Signature-compatible with `RapidSigner.sign` so the two are
        interchangeable at the call site; `iid` is part of that shared contract
        and unused here (the install id already rides in the signed query
        string, which is what gets hashed).

        The warm identity and the v46 static headers ride on the `for_v46`
        construction only. A `legacy` signer sends exactly what it sent before
        the `signer:` knob existed — no cookie, token, dm-status, captured UA,
        `sdk-version` or `x-bd-kmsv` — because inventing any of them would change
        the cold path's fingerprint, and leaking a live cookie into a v32
        signature would be invariant (c) on a real identity."""
        cfg = self._config
        sig = self._signature(url, device_id, payload)
        headers = {'User-Agent': self.user_agent(), 'x-argus': sig['x-argus'], 'x-ladon': sig['x-ladon'], 'x-gorgon': sig['x-gorgon'], 'x-khronos': str(sig['x-khronos']), 'x-ss-req-ticket': str(int(time.time() * 1000))}
        if not self._v46:
            return headers
        headers['sdk-version'] = SDK_VERSION_HEADER
        headers['x-bd-kmsv'] = BD_KMSV_HEADER
        if cfg.cookie:
            headers['cookie'] = cfg.cookie
            headers['x-tt-dm-status'] = DM_STATUS_LOGGED_IN
        if cfg.x_tt_token:
            headers['x-tt-token'] = cfg.x_tt_token
        return headers
