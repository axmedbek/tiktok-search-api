from __future__ import annotations

class TikTokSearchError(Exception):
    pass

class RateLimited(TikTokSearchError):
    pass

class SoftError(TikTokSearchError):
    pass

class TransportError(TikTokSearchError):
    pass

class PoolExhausted(TikTokSearchError):

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
