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
    # v46 signer params (must match the warm device's activated app version)
    sign_app_version: str = '46.0.42'
    sign_mssdk_ver_str: str = 'v05.01.02-alpha.7-ov-android'
    sign_mssdk_ver_code: str = '83952160'
    sign_license_id: str = '2142840551'
    # Warm identity (per-device): cookie/token from a logged-in app, plus the
    # full device query fingerprint (device_id/iid/cdid/openudid/region/...).
    cookie: str | None = None
    x_tt_token: str | None = None
    user_agent: str | None = None
    device_query: Mapping[str, Any] = field(default_factory=dict)

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
