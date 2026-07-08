import base64
import hashlib
import time
import random
from typing import Optional, Union
from urllib.parse import urlparse

from exception import InvalidEncryptionKey, InvalidURL
from helpers.argus import generate_protobuf, encode_argus_fn
from helpers.ladon import get_ladon_keys, encode_ladon
from native import reverse_bits

GORGON_TABLE = bytes.fromhex("ce7c47e421ff095cc9da81690147cba1ed6cc4b1")

# Clé par défaut (SIGN_KEY)
DEFAULT_SIGN_KEY = "wC8lD4bMTxmNVwY5jSkqi3QWmrphr/58ugLko7UZgWM="

class Metasec(object):

    def __init__(self, key: Optional[str] = None) -> None:
        # Utiliser SIGN_KEY par défaut si aucune clé fournie
        if key is None:
            key = DEFAULT_SIGN_KEY
            
        if not self._is_valid_key(key=key):
            raise InvalidEncryptionKey(f"The encryption key - {key} is incorrect")

        self._encryption_key = base64.b64decode(key)

    def sign(
            self,
            url: str,
            app_id: int,
            app_version: str,
            app_launch_time: int,
            device_type: str,
            sdk_version: str,
            sdk_version_code: int,
            license_id: int,
            device_id: Optional[str] = None,
            device_token: Optional[str] = None,
            dyn_seed: Optional[str] = None,
            dyn_version: Optional[int] = None,
            payload: Union[str, dict, bytes, None] = None,
            cookies: Optional[str] = None

    ) -> dict:
        if not self._is_valid_url(url=url):
            raise InvalidURL(f"The URL - {url} is incorrect")

        # Vérifier si l'URL contient des paramètres
        if "?" not in url:
            raise InvalidURL(f"The URL must contain query parameters: {url}")
        
        params = url.split("?")[1]

        params_bytes    = params.encode("utf-8")
        payload_bytes   = bytes.fromhex(payload) if payload is not None else bytes.fromhex("00000000000000000000000000000000")
        cookies_bytes   = cookies.encode("utf-8") if cookies is not None else bytes.fromhex("00000000000000000000000000000000")

        ts = self._get_timestamp()

        gorgon = self.gorgon_encode(
            params=params_bytes,
            payload=payload_bytes,
            cookies=cookies_bytes,
            ts=ts,
            is_arm64=True
        )

        # Генерируем X-Ladon
        ladon = self.ladon_encode(
            app_id=app_id,
            license_id=license_id,
            ts=ts
        )

        # Генерируем X-Argus
        argus = self.argus_encode(
            params=params_bytes,
            payload=payload_bytes,
            ts=ts,
            app_id=app_id,
            app_version=app_version,
            app_launch_time=app_launch_time,
            device_type=device_type,
            sdk_version=sdk_version,
            sdk_version_code=sdk_version_code,
            license_id=license_id,
            device_id=device_id,
            device_token=device_token,
            dyn_seed=dyn_seed,
            dyn_version=dyn_version
        )

        return {
            "x-argus": argus,
            "x-ladon": ladon,
            "x-gorgon": gorgon,
            "x-khronos": ts
        }

    def argus_encode(
            self,
            params: bytes,
            payload: bytes,
            app_launch_time: int,
            device_type: str,
            ts: int,
            app_version: str,
            app_id: int,
            license_id: int,
            sdk_version: str,
            sdk_version_code: int,
            device_id: Optional[str] = None,
            device_token: Optional[str] = None,
            dyn_seed: Optional[str] = None,
            dyn_version: Optional[int] = None,

    ) -> str:
        protobuf = generate_protobuf(
            params=params,
            payload=payload,
            ts=ts,
            app_id=app_id,
            app_version=app_version,
            app_launch_time=app_launch_time,
            device_type=device_type,
            sdk_version=sdk_version,
            sdk_version_code=sdk_version_code,
            license_id=license_id,
            device_id=device_id,
            device_token=device_token,
            dyn_seed=dyn_seed,
            dyn_version=dyn_version,
        )
        argus = encode_argus_fn(protobuf=protobuf, sign_key=self._encryption_key)
        return argus

    @staticmethod
    def ladon_encode(
            app_id: int,
            license_id: int,
            ts: int
    ) -> str:
        signature = f"{ts}-{license_id}-{app_id}".encode("utf-8")
        fill = 32 - len(signature)

        buffer = list(signature)

        for i in range(fill):
            buffer.append(fill)

        random_bytes = (random.randint(0, 0x7fffffff)).to_bytes(4, byteorder="little")
        app_id_encoded = hashlib.md5(random_bytes + str(app_id).encode("utf-8")).digest()

        key_list = get_ladon_keys(app_id=app_id_encoded)

        f, s = encode_ladon(
            key_list,
            int.from_bytes(buffer[4:8], byteorder="little"),
            int.from_bytes(buffer[8:12], byteorder="little"),
            int.from_bytes(buffer[12:16], byteorder="little"),
            int.from_bytes(buffer[0:4], byteorder="little")
        )
        encoded = f + s

        f, s = encode_ladon(
            key_list,
            int.from_bytes(buffer[20:24], byteorder="little"),
            int.from_bytes(buffer[24:28], byteorder="little"),
            int.from_bytes(buffer[28:32], byteorder="little"),
            int.from_bytes(buffer[16:20], byteorder="little")
        )

        encoded = encoded + f + s
        return base64.b64encode(random_bytes + encoded).decode()

    @staticmethod
    def gorgon_encode(
            params: bytes,
            payload: bytes,
            cookies: bytes,
            ts: int,
            is_arm64: bool = True
    ) -> str:
        ts_bytes = ts.to_bytes(4, byteorder="big")
        
        # S'assurer que params, payload et cookies font au moins 4 bytes
        params_padded = (params + b'\x00' * 4)[:4]
        payload_padded = (payload + b'\x00' * 4)[:4]
        cookies_padded = (cookies + b'\x00' * 4)[:4]
        
        buffer = bytearray(params_padded + payload_padded + cookies_padded + bytes.fromhex("20040204") + ts_bytes)

        for i in range(0, len(buffer)):
            buffer[i] ^= GORGON_TABLE[i]

        for i in range(0, len(buffer) - 1):
            buffer[i] = ((buffer[i] >> 4) & 0xf) | (buffer[i] << 4) & 0xff
            buffer[i] ^= buffer[i + 1]

        for i in range(0, len(buffer) - 1):
            buffer[i] = reverse_bits(buffer[i]) ^ 0xeb

        buffer[19] = ((buffer[19] >> 4) & 0xf) | (buffer[19] << 4) & 0xff
        buffer[19] ^= buffer[0]
        buffer[19] = reverse_bits(buffer[19]) ^ 0xeb

        if is_arm64:
            return (bytes.fromhex(f"8404a0ae1000") + buffer).hex()

        return (bytes.fromhex(f"0404a0ae1000") + buffer).hex()

    @staticmethod
    def _is_valid_key(
            key: str
    ) -> bool:
        try:
            is_base64 = base64.b64decode(key, validate=True)
            if len(is_base64) != 32:
                return False
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _is_valid_url(
            url: str
    ) -> bool:
        parsed_url = urlparse(url)
        return bool(parsed_url.scheme) and bool(parsed_url.netloc)

    @staticmethod
    def _get_timestamp() -> int:
        return round(time.time())