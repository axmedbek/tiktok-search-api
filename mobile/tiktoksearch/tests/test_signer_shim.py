"""Unit tests for the `tiktok_signer` package shim's class-identity guard.

The vendored modules import each other FLATLY (`from exception import ...`,
`from protobuf.protobuf import ProtoBuf`) through the sys.path shim in
`tiktok_signer/__init__.py`. Importing the same files by their package path
therefore produces a SECOND module object holding DIFFERENT class objects, and
an `except` on those never fires. Everything `signing.py` catches — and
therefore the whole `TransportError` → 502 → narrow-fallback contract — rests on
the re-exported classes being the ones the vendored code really raises.

That is the failure mode these tests exist for: it is silent. A re-vendor that
switches one module to package-relative imports would leave every `except` in
`signing.py` dead, and a signing failure would surface as an unmapped HTTP 500.
Nothing here touches the network; the subprocess check runs the same
interpreter on a COPY of the package in a temp dir.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tiktoksearch.tiktok_signer.exception as package_path_exception  # noqa: E402
from tiktoksearch.signing import _SIGNING_FAILURES  # noqa: E402
from tiktoksearch.tiktok_signer import (  # noqa: E402
    Metasec,
    MetasecBaseException,
    ProtoError,
)

VENDORED = Path(__file__).resolve().parents[1] / 'tiktok_signer'
# The flat import the shim depends on, and the line the guard defends.
FLAT_IMPORT = 'from exception import InvalidEncryptionKey, InvalidURL'
# A second, independently-loaded copy of `exception.py` — what a re-vendor using
# package-relative imports would effectively produce.
DOUBLE_LOAD = '''
import importlib.util as _ilu, os as _o
_spec = _ilu.spec_from_file_location(
    'exception_dup', _o.path.join(_o.path.dirname(__file__), 'exception.py'))
_dup = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_dup)
InvalidEncryptionKey, InvalidURL = _dup.InvalidEncryptionKey, _dup.InvalidURL
'''
GUARD_MESSAGE = 'tiktok_signer shim is broken'

# The modules the vendored code actually raises out of, reached through the
# objects handed to callers rather than by importing anything afresh.
METASEC_MODULE = sys.modules[Metasec.__module__]
ARGUS_MODULE = sys.modules[METASEC_MODULE.generate_protobuf.__module__]
PROTOBUF_MODULE = sys.modules[ARGUS_MODULE.ProtoBuf.__module__]


def _copy_package(dest: Path) -> Path:
    shutil.copytree(VENDORED, dest / 'tiktok_signer',
                    ignore=shutil.ignore_patterns('__pycache__'))
    return dest / 'tiktok_signer'


def _import_in_subprocess(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, '-c', 'import tiktok_signer'],
                          cwd=str(cwd), capture_output=True, text=True)


class TestReExportsAreTheVendoredClasses:
    def test_the_reexported_base_catches_what_metasec_raises(self):
        # The real vendored raise, not a stand-in.
        with pytest.raises(MetasecBaseException):
            Metasec().sign(url='bad', app_id=1233, app_version='46.0.42',
                           app_launch_time=1, device_type='SM-X', sdk_version='v',
                           sdk_version_code=1, license_id=1)

    def test_metasecs_own_exception_classes_derive_from_the_reexported_base(self):
        assert issubclass(METASEC_MODULE.InvalidURL, MetasecBaseException)
        assert issubclass(METASEC_MODULE.InvalidEncryptionKey, MetasecBaseException)

    def test_the_reexported_proto_error_is_the_encoders_own_class(self):
        assert PROTOBUF_MODULE.ProtoError is ProtoError

    def test_proto_error_is_not_a_metasec_exception(self):
        # Which is why `_SIGNING_FAILURES` has to name it separately: it lives
        # in the vendored protobuf helper and has its own base.
        assert not issubclass(ProtoError, MetasecBaseException)
        assert ProtoError in _SIGNING_FAILURES

    def test_both_reexports_are_what_signing_catches(self):
        assert MetasecBaseException in _SIGNING_FAILURES
        assert issubclass(METASEC_MODULE.InvalidURL, _SIGNING_FAILURES)


class TestTheTwoModuleObjectsTrapIsReal:
    """Not a hypothetical: the package path really does yield other classes."""

    def test_the_package_path_import_is_a_different_module_object(self):
        assert package_path_exception is not METASEC_MODULE
        assert package_path_exception.__name__ != 'exception'

    def test_the_package_path_base_is_a_different_class_object(self):
        assert package_path_exception.MetasecBaseException is not MetasecBaseException

    def test_an_except_on_the_package_path_copy_would_never_fire(self):
        # Both directions: neither hierarchy can catch the other's raise.
        assert not issubclass(package_path_exception.InvalidURL, MetasecBaseException)
        assert not issubclass(METASEC_MODULE.InvalidURL,
                              package_path_exception.MetasecBaseException)

    def test_a_package_path_exception_escapes_the_signing_handler(self):
        # The exact shape of the silent regression: an unmapped HTTP 500 rather
        # than a TransportError, no 502 and no fallback. This is what the
        # import-time guard exists to make impossible.
        assert not issubclass(package_path_exception.InvalidURL, _SIGNING_FAILURES)


class TestTheGuardFires:
    """A guard that cannot fail is not a guard."""

    def test_the_flat_import_the_guard_depends_on_is_still_there(self):
        # If this line moves, the simulation below stops simulating anything.
        assert FLAT_IMPORT in (VENDORED / 'metasec.py').read_text(encoding='utf-8')

    def test_an_unmodified_copy_imports_cleanly(self, tmp_path: Path):
        # The control: the guard is not a false positive.
        control = tmp_path / 'control'
        control.mkdir()
        _copy_package(control)
        done = _import_in_subprocess(control)
        assert done.returncode == 0, done.stderr

    def test_a_simulated_revendor_is_refused_at_import_time(self, tmp_path: Path):
        copied = _copy_package(tmp_path)
        metasec = copied / 'metasec.py'
        metasec.write_text(
            metasec.read_text(encoding='utf-8').replace(FLAT_IMPORT, DOUBLE_LOAD),
            encoding='utf-8',
        )
        done = _import_in_subprocess(tmp_path)
        assert done.returncode != 0
        assert GUARD_MESSAGE in done.stderr
