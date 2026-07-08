"""TikTok mobile request signer (X-Argus / X-Gorgon / X-Ladon / X-Khronos).

Vendored from the open-source `armxe/tiktok-api` repo (Mobile/ package). The
modules use flat intra-package imports (`from native import ...`,
`from helpers.argus import ...`) that only resolve when this package's own
directory is on sys.path — so we prepend it here, making
`from tiktok_signer import Metasec` work from anywhere without callers touching
sys.path.

This is a pure-Python reimplementation of TikTok's client-side signing; it needs
no phone and no external service. See mobile/README_signer_api.md.
"""

import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)

from metasec import Metasec  # noqa: E402  (resolved via the sys.path shim above)
from ttencrypt import TT  # noqa: E402  (TikTok body encryption, for device register)

__all__ = ["Metasec", "TT"]
