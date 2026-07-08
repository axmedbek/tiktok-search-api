from Crypto.Cipher import AES


class AES(object):
    def __init__(self, key: bytes, iv: bytes):
        self._bs = AES.block_size
        self._key = key
        self._iv = iv

    def encrypt(self, raw: bytes, mode: int = AES.MODE_OFB) -> bytes:
        raw = self._pad(raw)
        cipher = AES.new(self._key, mode, self._iv)
        result = cipher.encrypt(bytes(raw))

        return result

    def decrypt(self, enc: bytes, mode: int = AES.MODE_OFB) -> bytes:
        cipher = AES.new(self._key, mode, self._iv)
        result = self._unpad(cipher.decrypt(enc))

        return result

    @staticmethod
    def _pad(s: bytes) -> bytes:
        fill_number = 16 - (len(s) % 16)
        for i in range(fill_number):
            s += fill_number.to_bytes(1, byteorder="big")

        return s

    @staticmethod
    def _unpad(s: bytes) -> bytes:
        return s[: -ord(s[len(s) - 1:])]