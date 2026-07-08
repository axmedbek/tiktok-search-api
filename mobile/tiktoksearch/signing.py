from __future__ import annotations
import random
import time
from .config import ClientConfig
from .tiktok_signer import Metasec

class MetasecSigner:

    def __init__(self, config: ClientConfig, *, launch_time: int | None=None) -> None:
        self._config = config
        self._metasec = Metasec()
        self._launch_time = launch_time or int(time.time()) - random.randint(600, 7200)

    def user_agent(self) -> str:
        cfg = self._config
        return f'com.zhiliaoapp.musically/{cfg.version_code} (Linux; U; Android {cfg.os_version}; en; {cfg.device_type}; Build/RP1A.200720.012; Cronet/TTNetVersion:)'

    def sign(self, *, url: str, device_id: str, payload: bytes | None=None) -> dict[str, str]:
        cfg = self._config
        sig = self._metasec.sign(url=url, app_id=cfg.app_id, app_version=cfg.app_version, app_launch_time=self._launch_time, device_type=cfg.device_type, sdk_version=cfg.sdk_version, sdk_version_code=cfg.sdk_version_code, license_id=cfg.license_id, device_id=device_id, device_token='', payload=payload.hex() if isinstance(payload, (bytes, bytearray)) else payload)
        return {'User-Agent': self.user_agent(), 'x-argus': sig['x-argus'], 'x-ladon': sig['x-ladon'], 'x-gorgon': sig['x-gorgon'], 'x-khronos': str(sig['x-khronos']), 'x-ss-req-ticket': str(int(time.time() * 1000))}
