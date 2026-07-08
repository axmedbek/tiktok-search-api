from __future__ import annotations
import os
from dataclasses import dataclass, field, fields, replace
from typing import Any, Mapping
import yaml
DEFAULT_HOSTS: tuple[str, ...] = ('https://api16-normal-c-useast1a.tiktokv.com', 'https://api16-normal-c-useast2a.tiktokv.com', 'https://api19-normal-c-useast1a.tiktokv.com')

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

    @classmethod
    def _field_names(cls) -> frozenset[str]:
        return frozenset((f.name for f in fields(cls)))

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> 'ClientConfig':
        known = cls._field_names()
        data = {k: v for k, v in cfg.items() if k in known}
        if 'api_hosts' in data and data['api_hosts']:
            data['api_hosts'] = tuple(data['api_hosts'])
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
    proxies: tuple[str, ...] = ()
    devices: tuple[Mapping[str, Any], ...] = ()
    synthetic_devices: int = 0
    client_defaults: ClientConfig = field(default_factory=ClientConfig)

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> 'PoolConfig':
        return cls(daily_request_cap_per_device=int(cfg.get('daily_request_cap_per_device', 300)), acquire_timeout_s=float(cfg.get('acquire_timeout_s', 60)), max_results_per_search=int(cfg.get('max_results_per_search', 60)), proxies=tuple((p for p in cfg.get('proxies') or [] if p)), devices=tuple(cfg.get('devices') or ()), synthetic_devices=int(cfg.get('synthetic_devices', 0)), client_defaults=ClientConfig.from_mapping(cfg))

    @classmethod
    def load_yaml(cls, path: str | os.PathLike[str]) -> 'PoolConfig':
        if not os.path.exists(path):
            return cls()
        with open(path, 'r', encoding='utf-8') as f:
            return cls.from_mapping(yaml.safe_load(f) or {})
