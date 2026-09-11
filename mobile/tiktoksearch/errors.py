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

class GatewayRefused(TransportError):
    """The request reached a TikTok gateway and was refused with NO body:
    HTTP 200, zero bytes, `tt_orcas_res: 1`.

    Its own class because this project has twice mis-read it, at opposite
    extremes, and both readings cost weeks:

    * It is NOT `hit_shark`. Risk-control answers 200 with a well-formed JSON
      body carrying `status_code: 0` and an empty item list; a zero-length body
      never reached the application layer at all. So this is a `TransportError`
      subclass and NOT a `SoftError` — `pool.run` reports every escaping
      `SoftError` as an empty, and charging identity health for this would
      retire a warm identity that did nothing wrong (anti-block invariant (b)).
    * It is NOT proof of a bad signature either. Measured 2026-09-11: the app's
      OWN request, carrying a signature known good on the posts endpoint at the
      same moment, got this shape on `/tiktok/user/profile/other/v1`. The cause
      is per-endpoint and must be established per endpoint — a wrong host, a
      rejected signature, or (inferred, not measured, for profile) a logged-out
      session are all live candidates.

    What it does assert is only what was observed: the gateway answered, and it
    answered with nothing. Everything past that is a diagnosis to be made with
    a measurement, not inherited from this class."""

class NotFound(TikTokSearchError):
    """TikTok answered that the requested user does not exist / is deleted.

    Deliberately NOT a `SoftError`: re-signing cannot make a deleted user
    exist, so this is raised on the first reply with no retries, and it must
    never be reported as an empty against identity health — a healthy warm
    identity is not at fault for a username the caller made up. Maps to HTTP
    404 in `api/app.py`."""

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
