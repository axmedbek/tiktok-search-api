"""RapidAPI-backed v46 signer.

The vendored pure-Python MetasecSigner produces a v37-era x-argus that TikTok's
v46 risk-control ("Shark") rejects to an empty result. The RapidAPI signer
(`tiktok-api-signer`) returns a v46-consistent x-argus/x-gorgon/x-ladon/x-khronos
that, paired with a warm v46 device identity, returns real search results.

See memory: direct-api-WORKS, signer-repos-surveyed-v46.
"""
from __future__ import annotations
import base64
import logging
import requests
from .config import ClientConfig
from .errors import RateLimited, TransportError

logger = logging.getLogger('tiktoksearch.rapid_signer')


class RapidSigner:
    """Signs a request URL via the RapidAPI TikTok signer, returning the
    x-argus/x-gorgon/x-ladon/x-khronos headers TikTok's v46 backend accepts."""

    def __init__(self, config: ClientConfig) -> None:
        if not config.rapidapi_key:
            raise ValueError('RapidSigner requires config.rapidapi_key')
        self._config = config
        self._session = requests.Session()

    def user_agent(self) -> str:
        cfg = self._config
        if cfg.user_agent:
            return cfg.user_agent
        return (f'com.zhiliaoapp.musically/2024600420 (Linux; U; Android {cfg.os_version}; '
                f'en_US; {cfg.device_type}; Build/BE2A.250530.026.F3; Cronet/TTNetVersion:)')

    def _post(self, path: str, body: dict) -> dict:
        cfg = self._config
        try:
            resp = self._session.post(
                f'https://{cfg.rapidapi_host}{path}',
                headers={
                    'Content-Type': 'application/json',
                    'x-rapidapi-host': cfg.rapidapi_host,
                    'x-rapidapi-key': cfg.rapidapi_key,
                },
                json=body,
                timeout=cfg.request_timeout_s,
            )
        except requests.RequestException as exc:
            raise TransportError(f'RapidAPI signer request failed: {exc}') from exc
        if resp.status_code == 429:
            raise RateLimited(f'RapidAPI signer quota exceeded — upgrade plan or wait: {resp.text[:160]}')
        if resp.status_code != 200:
            raise TransportError(f'RapidAPI signer HTTP {resp.status_code}: {resp.text[:200]}')
        try:
            return resp.json()
        except ValueError as exc:
            raise TransportError(f'RapidAPI signer returned non-JSON: {resp.text[:200]}') from exc

    def _sign_tiktanic(self, url: str, device_id: str, payload: bytes | None) -> dict[str, str]:
        cfg = self._config
        sig = self._post('/android/get_sign', {
            'url': url,
            'dev_info': {
                'app_id': str(cfg.app_id), 'mssdk_ver_str': cfg.sign_mssdk_ver_str,
                'device_id': device_id, 'mssdk_ver_code': cfg.sign_mssdk_ver_code,
                'app_version': cfg.sign_app_version, 'channel': cfg.channel,
                'license_id': cfg.sign_license_id, 'device_type': cfg.device_type,
                'os': 'Android', 'os_version': cfg.os_version,
                'sec_device_id_token': '', 'lanusk': '', 'lanusv': '', 'seed': '', 'seed_algorithm': '',
            },
            'payload': base64.b64encode(payload or b'').decode(),
        })
        missing = [k for k in ('x-argus', 'x-gorgon', 'x-ladon', 'x-khronos') if k not in sig]
        if missing:
            raise TransportError(f'RapidAPI signer missing headers {missing}: {sig}')
        return {'x-argus': sig['x-argus'], 'x-ladon': sig['x-ladon'],
                'x-gorgon': sig['x-gorgon'], 'x-khronos': str(sig['x-khronos'])}

    def _sign_working(self, url: str, device_id: str, iid: str) -> dict[str, str]:
        cfg = self._config
        out = self._post('/sign', {
            'url': url, 'os_version': cfg.os_version, 'device_model': cfg.device_type,
            'device_id': device_id, 'install_id': iid, 'tiktok_version': '',
            'headers': {'sdk-version': '2', 'user-agent': self.user_agent(),
                        'cookie': cfg.cookie or '', 'x-tt-token': cfg.x_tt_token or '',
                        'accept-encoding': 'gzip'},
            'cookies': {},
        })
        data = out.get('data') or {}
        if data.get('raw') == 'ERROR' or 'X-Argus' not in data:
            raise TransportError(f'tiktok-signer-working error: {out.get("message") or out}')
        return {'x-argus': data['X-Argus'], 'x-ladon': data['X-Ladon'],
                'x-gorgon': data['X-Gorgon'], 'x-khronos': str(data['X-Khronos'])}

    def sign(self, *, url: str, device_id: str, payload: bytes | None = None,
             iid: str = '') -> dict[str, str]:
        cfg = self._config
        if cfg.rapidapi_provider == 'working':
            sig = self._sign_working(url, device_id, iid)
        else:
            sig = self._sign_tiktanic(url, device_id, payload)
        headers = {
            'User-Agent': self.user_agent(),
            'x-argus': sig['x-argus'], 'x-ladon': sig['x-ladon'],
            'x-gorgon': sig['x-gorgon'], 'x-khronos': sig['x-khronos'],
            'sdk-version': '2', 'x-bd-kmsv': '0',
        }
        if cfg.cookie:
            headers['cookie'] = cfg.cookie
            headers['x-tt-dm-status'] = 'login=1;ct=1;rt=1'
        if cfg.x_tt_token:
            headers['x-tt-token'] = cfg.x_tt_token
        return headers
