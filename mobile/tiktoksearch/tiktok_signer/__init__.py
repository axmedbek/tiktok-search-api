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

# Re-exported so callers can `except` what signing raises. They MUST be imported
# through the shim, like metasec.py and helpers/argus.py do, and not as
# `tiktoksearch.tiktok_signer.exception` / `...protobuf.protobuf`: importing the
# same file by both names creates two module objects with two distinct class
# hierarchies, and an `except` on the package-path copy would silently never
# fire. `ProtoError` is NOT a `MetasecBaseException` (it lives in the vendored
# protobuf helper, which has its own base), so it has to be named separately or
# a `None`-valued sign field escapes signing.py's handler as an HTTP 500.
from exception import MetasecBaseException  # noqa: E402
from protobuf.protobuf import ProtoError  # noqa: E402

# The re-exports are only useful if they are the SAME class objects the vendored
# code raises. A re-vendor that keeps this shim but switches any module to
# package-relative imports would load a second copy of `exception` /
# `protobuf.protobuf`, every `except` in signing.py would quietly stop firing,
# and a signing failure would surface as an unmapped HTTP 500 instead of a
# `TransportError`. Reach the classes through the objects actually handed to
# callers (never a fresh import) so that regression cannot be silent.
_metasec_mod = _sys.modules[Metasec.__module__]
_argus_mod = _sys.modules[_metasec_mod.generate_protobuf.__module__]
_proto_mod = _sys.modules[_argus_mod.ProtoBuf.__module__]
if not issubclass(_metasec_mod.InvalidURL, MetasecBaseException) or _proto_mod.ProtoError is not ProtoError:
    raise ImportError(
        'tiktok_signer shim is broken: the vendored modules raise exception '
        'classes other than the ones re-exported here, so signing.py could '
        'not catch them'
    )

__all__ = ["Metasec", "TT", "MetasecBaseException", "ProtoError"]
