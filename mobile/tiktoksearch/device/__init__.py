"""Device-driven harvesting: the genuine TikTok app as the data source.

Additive by construction. Nothing in here is imported by `api/app.py`,
`client.py`, `pool.py` or the search path, and nothing in here issues a signed
request — so the running search worker's behaviour is untouched whether this
package is present or not. It is reached only through
`broker/device_source.py`, which the worker builds only under
`--source device`.

`mobile/identities.json` is never read or written by anything in this package:
the app holds its own credentials and we only read what it fetched.
"""
from __future__ import annotations
from .driver import DeviceConfig, DeviceDriver, DeviceFeed, intent_argv, profile_uri, run_intent, session_env, validated_user_id
from .errors import DeviceError, HarvestTimeout, IntentFailed, UnreadableResponse, UnusableUserId

__all__ = ['DeviceConfig', 'DeviceDriver', 'DeviceFeed', 'DeviceError', 'HarvestTimeout', 'IntentFailed', 'UnreadableResponse', 'UnusableUserId', 'intent_argv', 'profile_uri', 'run_intent', 'session_env', 'validated_user_id']
