from __future__ import annotations
import os
from dataclasses import dataclass, field, fields, replace
from typing import Any, Mapping
import yaml
DEFAULT_HOSTS: tuple[str, ...] = ('https://api16-normal-c-useast1a.tiktokv.com', 'https://api16-normal-c-useast2a.tiktokv.com', 'https://api19-normal-c-useast1a.tiktokv.com')
# Env var that overrides the YAML `rapidapi_key`, so the key never has to be
# committed in a config profile (see .env.example). Empty/unset falls back to YAML.
RAPIDAPI_KEY_ENV = 'RAPIDAPI_KEY'
# --- Signer modes (the `signer:` config knob) ---------------------------------
# Which signer produces x-argus/x-gorgon/x-ladon/x-khronos, and therefore which
# request path the client takes.
#   local  — the vendored pure-Python MetasecSigner, fed the v46 `sign_*` params
#            and the warm identity. Direct path, no paid quota.
#   rapid  — the paid RapidAPI v46 signer. Direct path, as before.
#   legacy — MetasecSigner on the v32 defaults with no identity: the cold path
#            (api_hosts, count=20) that returns empty results by design.
SIGNER_LOCAL = 'local'
SIGNER_RAPID = 'rapid'
SIGNER_LEGACY = 'legacy'
SIGNER_MODES: frozenset[str] = frozenset((SIGNER_LOCAL, SIGNER_RAPID, SIGNER_LEGACY))
# Which `dyn_encode` scheme the captured `(f24, f3)` pair belongs to — the value
# that rides in the argus payload's f26.1. MEASURED for MSSDK v05.03.01 / app
# 46.9.1 by the 2026-09-11 spike: the app's own f26 decodes to
# `{1: 6, 2: h"438576747395d4cfe9eb4996"}`, and every scalar in this payload is
# stored shifted, so f26.1 = 6 is dyn_version 3. The spike further reports that
# the vendored `dyn_encode(dyn_version=3, ...)`, unmodified, reproduced that
# 12-byte f26.2 exactly and was the only one of its eight branches to do so —
# that second half is the spike's measurement, not one reproducible here, since
# it needs the capture's own params/payload/rand.
#
# A config knob all the same: it is MSSDK-scoped, so the next MSSDK bump can
# move it, and a live check must be able to correct it without a code change.
# Inert with no pair configured.
DEFAULT_DYN_VERSION = 3
# The `(f24, f3)` pair is identity material of the same class as the cookie.
# Named here because `__post_init__` and `from_mapping` both have to talk about
# the two fields as one unit.
DYN_PAIR_FIELDS: tuple[str, str] = ('dyn_seed', 'dyn_rand')

@dataclass(frozen=True, slots=True)
class ClientConfig:
    api_hosts: tuple[str, ...] = DEFAULT_HOSTS
    app_id: int = 1233
    app_version: str = '32.9.4'
    version_code: str = '320904'
    sdk_version: str = 'v04.04.09-boa-hotfix'
    sdk_version_code: int = 41090
    license_id: int = 11512
    device_type: str = 'SM-A207F'
    os_version: str = '11'
    channel: str = 'googleplay'
    request_timeout_s: float = 20.0
    retries: int = 2
    device_id: str | None = None
    iid: str | None = None
    proxy: str | None = None
    # --- Direct-API (RapidAPI-signed) mode ------------------------------
    # When rapidapi_key is set, the client signs each request via the RapidAPI
    # v46 signer and hits the search host directly (GET query-param endpoint),
    # instead of the vendored MetasecSigner. This is the path that actually
    # returns real paginated results (see memory: direct-api-WORKS). It needs a
    # WARM device identity (device_query below + at least a cookie or x_tt_token)
    # captured from a logged-in real app.
    rapidapi_key: str | None = None
    rapidapi_host: str = 'tiktok-api-signer.p.rapidapi.com'
    # Which RapidAPI signer schema to use. 'tiktanic' = tiktok-api-signer
    # (/android/get_sign, dev_info body). 'working' = tiktok-signer-working
    # (/sign, url+device_model+headers body, tracks v46.0.3). Switch providers
    # when one's monthly quota is exhausted.
    rapidapi_provider: str = 'tiktanic'
    # Which signer to use: one of SIGNER_MODES, or ''/None to derive it (see
    # resolved_signer). Setting `signer: rapid` restores the paid path with no
    # code change; `signer: local` is the default profile's choice. Optional
    # like rapidapi_key: a bare `signer:` key in YAML parses as None, which
    # from_mapping coerces to '' and resolved_signer treats as unset.
    signer: str | None = ''
    search_host: str = 'https://search19-normal-alisg.tiktokv.com'
    # Host for the USER-SCOPED endpoints (`/aweme/v1/aweme/post/` and
    # `/tiktok/user/profile/other/v1`). `search_host` serves only the search
    # paths and 404s these — measured 2026-09-10; this is the host the real app
    # calls them on. `client._host_for` is the only reader.
    posts_host: str = 'https://api32-core-alisg.tiktokv.com'
    # v46 signer params (must match the warm device's activated app version)
    #
    # `sign_app_version` is NOT enforced against the identity here — a frozen
    # config has no identity to compare with, and the identities hot-reload
    # under it. It is checked where the two meet and where a mismatch actually
    # costs something: `signing.MetasecSigner.for_v46` compares it against the
    # identity's own `device_query['version_name']` and logs a WARNING naming
    # both versions. A mismatch is anti-block invariant (c) — TikTok reads it as
    # hit_shark — so it must be visible, but it must not crash startup either:
    # `pool._build_slots` builds an identity-free synthetic client when
    # `identities.json` is missing, and raising would turn that into a boot
    # failure.
    sign_app_version: str = '46.0.42'
    sign_mssdk_ver_str: str = 'v05.03.01-ov-android'
    sign_mssdk_ver_code: str = '84082976'
    sign_license_id: str = '2142840551'
    # Warm identity (per-device): cookie/token from a logged-in app, plus the
    # full device query fingerprint (device_id/iid/cdid/openudid/region/...).
    cookie: str | None = None
    x_tt_token: str | None = None
    user_agent: str | None = None
    device_query: Mapping[str, Any] = field(default_factory=dict)
    # The captured argus `(f24 dyn_seed, f3 rand)` PAIR — the one remaining
    # capture dependency for the user-scoped endpoints. SECRET, in the same
    # class as `cookie`: never logged, never returned, masked first6…last4 if a
    # diagnostic must name it. `dyn_rand` is f3 and is also the `rand` the
    # signer feeds to `dyn_encode`, so one value covers both and they cannot
    # drift. Both or neither — see `__post_init__`.
    dyn_seed: str | None = None
    dyn_rand: int | None = None
    dyn_version: int = DEFAULT_DYN_VERSION

    def __post_init__(self) -> None:
        """Reject a HALF pair, at the only boundary that can.

        A `dyn_seed` with no `dyn_rand` is not a partially useful identity: a
        good f24 beside a freely chosen f3 was MEASURED to be refused with a
        zero-byte body, i.e. it fails exactly like no pair at all while looking
        configured. Raising here makes a half-paired config impossible to hold
        rather than merely discouraged, so no signer, pool or client has to
        re-check it. The message names neither value."""
        seed, rand = self.dyn_seed, self.dyn_rand
        if bool(seed) != (rand is not None):
            have, missing = DYN_PAIR_FIELDS if seed else DYN_PAIR_FIELDS[::-1]
            raise ValueError(f'{have} is set without {missing}: the captured argus pair is one unit — configure both or neither')

    def resolved_signer(self) -> str:
        """The signer mode this config actually runs, as one of SIGNER_MODES.

        An explicit `signer:` wins. An empty value (unset, or a bare `signer:`
        key) DERIVES the mode the way the client used to hard-code it — `rapid`
        when a rapidapi_key is configured, else `legacy` — so a profile that
        never mentions `signer:` behaves exactly as it did before the knob
        existed. A value outside SIGNER_MODES cannot arrive from YAML
        (`from_mapping` rejects it); reaching here it derives too, rather than
        crashing a directly-constructed config.
        `config_signed.yaml` (no key) depends on that: it must keep resolving to
        the cold legacy path. Pure: the dataclass is frozen, nothing is stored."""
        mode = self.signer.strip().lower() if self.signer else ''
        if mode in SIGNER_MODES:
            return mode
        return SIGNER_RAPID if self.rapidapi_key else SIGNER_LEGACY

    @classmethod
    def _field_names(cls) -> frozenset[str]:
        return frozenset((f.name for f in fields(cls)))

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> 'ClientConfig':
        known = cls._field_names()
        data = {k: v for k, v in cfg.items() if k in known}
        if 'api_hosts' in data and data['api_hosts']:
            data['api_hosts'] = tuple(data['api_hosts'])
        if 'signer' in data:
            # Validated at the YAML boundary, because resolved_signer() DERIVES
            # on anything it does not recognise: a typo like `signer: locl` on a
            # profile that has a rapidapi_key would otherwise resolve silently to
            # `rapid` and spend money on every request. A bare `signer:` (None)
            # is a legitimate "unset" and coerces to ''.
            mode = str(data['signer']).strip().lower() if data['signer'] is not None else ''
            if mode and mode not in SIGNER_MODES:
                raise ValueError(f'unknown signer {mode!r}: expected one of {sorted(SIGNER_MODES)}, or empty to derive it from rapidapi_key')
            data['signer'] = mode
        for name in ('dyn_rand', 'dyn_version'):
            # YAML hands back a str for a quoted number, and these are shifted
            # into a protobuf varint: coerced at the boundary (per
            # .claude/rules/code-standards.md) so a quoted value cannot reach
            # the signer as a string and fail inside the crypto. A bare
            # `dyn_rand:` parses as None, which is a legitimate "unset" and is
            # left for __post_init__ to pair-check; a bare `dyn_version:` falls
            # back to the module default rather than becoming None.
            if name in data and data[name] is not None:
                data[name] = int(data[name])
            elif name == 'dyn_version' and name in data:
                del data[name]
        env_key = os.environ.get(RAPIDAPI_KEY_ENV, '').strip()
        if env_key:
            data['rapidapi_key'] = env_key
        return cls(**data)

    def with_overrides(self, device_cfg: Mapping[str, Any]) -> 'ClientConfig':
        known = self._field_names()
        overrides = {k: v for k, v in device_cfg.items() if k in known and v not in (None, '')}
        if 'api_hosts' in overrides:
            overrides['api_hosts'] = tuple(overrides['api_hosts'])
        return replace(self, **overrides)

@dataclass(frozen=True, slots=True)
class PoolConfig:
    daily_request_cap_per_device: int = 300
    acquire_timeout_s: float = 60.0
    max_results_per_search: int = 60
    default_fan_out: int = 1
    proxies: tuple[str, ...] = ()
    devices: tuple[Mapping[str, Any], ...] = ()
    synthetic_devices: int = 0
    client_defaults: ClientConfig = field(default_factory=ClientConfig)

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> 'PoolConfig':
        return cls(daily_request_cap_per_device=int(cfg.get('daily_request_cap_per_device', 300)), acquire_timeout_s=float(cfg.get('acquire_timeout_s', 60)), max_results_per_search=int(cfg.get('max_results_per_search', 60)), default_fan_out=int(cfg.get('default_fan_out', 1)), proxies=tuple((p for p in cfg.get('proxies') or [] if p)), devices=tuple(cfg.get('devices') or ()), synthetic_devices=int(cfg.get('synthetic_devices', 0)), client_defaults=ClientConfig.from_mapping(cfg))

    @classmethod
    def load_yaml(cls, path: str | os.PathLike[str]) -> 'PoolConfig':
        if not os.path.exists(path):
            return cls()
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_mapping(yaml.safe_load(f) or {})
