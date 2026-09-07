from __future__ import annotations
from enum import Enum

class TikTokSearchError(Exception):
    pass

class RateLimited(TikTokSearchError):
    pass

class SoftError(TikTokSearchError):
    pass

class TransportError(TikTokSearchError):
    pass

class PoolCode(str, Enum):
    """Why the pool could not serve a request.

    The HTTP mapping in `api/app.py` switches on this, never on the prose of
    `reason` — rewording a message must not be able to flip a status code."""
    CAP = 'cap'
    BUSY = 'busy'
    GONE = 'gone'
    STALE = 'stale'

class PoolExhausted(TikTokSearchError):

    def __init__(self, reason: str, *, code: PoolCode = PoolCode.BUSY) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
