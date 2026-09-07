"""Unit tests for signing.py: the v46 param mapping, the two header sets, and
what a signing failure is allowed to become.

The local signer's crypto is pure Python and runs IN-PROCESS, so a real
signature costs nothing and touches no socket — several tests here therefore
sign for real, because that is the only way the v46-vs-v32 mapping and the
vendored `ProtoError` become observable. `MetasecSpy` is used where the
ASSERTION is about the arguments handed to the vendored object rather than about
the bytes that come back. Nothing here reads `mobile/identities.json`: the
identity material is the synthetic fake cookie/token/UA from conftest.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import logging
import struct
import sys
import zlib
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    FAKE_COOKIE,
    FAKE_TOKEN,
    FAKE_UA,
    MetasecSpy,
    local_config,
)

from tiktoksearch.config import (  # noqa: E402
    SIGNER_LEGACY,
    SIGNER_LOCAL,
    ClientConfig,
)
from tiktoksearch.errors import TransportError  # noqa: E402
from tiktoksearch.signing import (  # noqa: E402
    BD_KMSV_HEADER,
    DM_STATUS_LOGGED_IN,
    LOCAL_NO_UA_MSG,
    SDK_VERSION_HEADER,
    SIGNATURE_KEYS,
    _SIGNING_FAILURES,
    MetasecSigner,
    v46_params,
)
from tiktoksearch.tiktok_signer import (  # noqa: E402
    Metasec,
    MetasecBaseException,
    ProtoError,
)

SIGNING_LOGGER = 'tiktoksearch.signing'
# The vendored exceptions as the SHIM loaded them, i.e. as signing.py's handler
# sees them. Reached through the module of the class actually handed to callers,
# never by importing `tiktoksearch.tiktok_signer.exception` — that path yields a
# second module object with different class objects (see test_signer_shim.py).
InvalidEncryptionKey = sys.modules[Metasec.__module__].InvalidEncryptionKey

# The four values the v46 signer must be handed, written out as literals rather
# than read back off the config: the ONE bug this Epic fixed was handing the
# signer the v32 defaults below, and a test that reads `cfg.sign_*` would follow
# a regression instead of catching it.
V46_APP_VERSION = '46.0.42'
V46_SDK_VERSION = 'v05.01.02-alpha.7-ov-android'
V46_SDK_VERSION_CODE = 83952160
V46_LICENSE_ID = 2142840551
V32_APP_VERSION = '32.9.4'
V32_SDK_VERSION = 'v04.04.09-boa-hotfix'
V32_SDK_VERSION_CODE = 41090
V32_LICENSE_ID = 11512
V32_VERSION_CODE = '320904'

SIGN_FIELDS = ('app_version', 'sdk_version', 'sdk_version_code', 'license_id')
V46_HANDED = {'app_version': V46_APP_VERSION, 'sdk_version': V46_SDK_VERSION,
              'sdk_version_code': V46_SDK_VERSION_CODE, 'license_id': V46_LICENSE_ID}
V32_HANDED = {'app_version': V32_APP_VERSION, 'sdk_version': V32_SDK_VERSION,
              'sdk_version_code': V32_SDK_VERSION_CODE, 'license_id': V32_LICENSE_ID}

# Every signed request carries these six, whichever signer produced them.
BASE_KEYS = frozenset({'User-Agent', 'x-argus', 'x-ladon', 'x-gorgon', 'x-khronos',
                       'x-ss-req-ticket'})
# What `for_v46` — and only `for_v46` — adds on top.
V46_KEYS = frozenset({'sdk-version', 'x-bd-kmsv', 'cookie', 'x-tt-dm-status',
                      'x-tt-token'})

# A realistically shaped signed search URL. Never requested — only signed.
URL = ('https://search19-normal-alisg.tiktokv.com/aweme/v1/search/item/'
       '?keyword=ocean&count=10&device_id=FAKE-DEV-1&iid=FAKE-IID-1&aid=1233')
LAUNCH = 1_700_000_000
# A marker that must never survive into an error message.
LEAK = 'LEAKED-SECRET-MATERIAL'


def _spying(signer: MetasecSigner, *, raises: BaseException | None = None) -> MetasecSpy:
    spy = MetasecSpy(raises=raises)
    signer._metasec = spy
    return spy


def _handed(spy: MetasecSpy) -> dict:
    return {k: spy.seen[k] for k in SIGN_FIELDS}


def _sign_local(signer: MetasecSigner) -> dict[str, str]:
    return signer.sign(url=URL, device_id='FAKE-DEV-1', iid='FAKE-IID-1')


class TestV46ParamMapping:
    """The crux: `app_version`/`sdk_version`/`sdk_version_code`/`license_id` are
    ARGUMENTS to `Metasec.sign`, and `ClientConfig`'s defaults for them are the
    v32 ones. Feeding those to a v46 warm identity is what made the vendored
    signer look dead."""

    def test_v46_params_promotes_the_sign_fields(self):
        mapped = v46_params(ClientConfig(signer=SIGNER_LOCAL))
        assert (mapped.app_version, mapped.sdk_version, mapped.sdk_version_code,
                mapped.license_id) == (V46_APP_VERSION, V46_SDK_VERSION,
                                       V46_SDK_VERSION_CODE, V46_LICENSE_ID)

    def test_v46_params_coerces_the_numeric_fields_to_int(self):
        # The `sign_*` fields are strings in YAML; `Metasec.sign` packs them.
        mapped = v46_params(ClientConfig(signer=SIGNER_LOCAL))
        assert isinstance(mapped.sdk_version_code, int)
        assert isinstance(mapped.license_id, int)

    def test_v46_params_does_not_mutate_the_source_config(self):
        source = ClientConfig(signer=SIGNER_LOCAL)
        mapped = v46_params(source)
        assert mapped is not source
        assert (source.app_version, source.sdk_version, source.sdk_version_code,
                source.license_id) == (V32_APP_VERSION, V32_SDK_VERSION,
                                       V32_SDK_VERSION_CODE, V32_LICENSE_ID)

    def test_values_handed_to_metasec_sign_are_the_v46_ones(self):
        # The assertion the whole Epic rests on, taken at the boundary rather
        # than from the config: this is what the signer actually signs with.
        signer = MetasecSigner.for_v46(local_config())
        spy = _spying(signer)
        _sign_local(signer)
        assert _handed(spy) == V46_HANDED
        assert _handed(spy) != V32_HANDED

    def test_legacy_construction_still_hands_the_v32_defaults(self):
        signer = MetasecSigner(local_config(signer=SIGNER_LEGACY))
        spy = _spying(signer)
        signer.sign(url=URL, device_id='FAKE-DEV-1')
        assert _handed(spy) == V32_HANDED

    def test_only_for_v46_marks_a_signer_as_v46(self):
        # Plain construction stays cold EVEN on a config carrying an identity.
        assert MetasecSigner(local_config())._v46 is False
        assert MetasecSigner.for_v46(local_config())._v46 is True

    def test_v46_and_v32_params_sign_the_same_url_differently(self):
        # Real crypto, no stub: makes the mapping observable rather than merely
        # asserted, so a silent revert to the v32 defaults cannot pass.
        v46 = MetasecSigner.for_v46(local_config(), launch_time=LAUNCH)
        v32 = MetasecSigner(local_config(), launch_time=LAUNCH)
        assert _sign_local(v46)['x-argus'] != v32.sign(
            url=URL, device_id='FAKE-DEV-1'
        )['x-argus']


class TestRealLocalSignature:
    """The vendored signer, run for real in-process. No network: a signature is
    a computation, and conftest's tripwire still guards the sockets."""

    def test_real_signing_produces_the_full_v46_header_set(self):
        headers = _sign_local(MetasecSigner.for_v46(local_config(), launch_time=LAUNCH))
        assert set(headers) == BASE_KEYS | V46_KEYS
        assert all(headers[k] for k in SIGNATURE_KEYS)

    def test_real_signing_carries_the_warm_identity(self):
        headers = _sign_local(MetasecSigner.for_v46(local_config(), launch_time=LAUNCH))
        assert headers['cookie'] == FAKE_COOKIE
        assert headers['x-tt-token'] == FAKE_TOKEN
        assert headers['User-Agent'] == FAKE_UA


class TestLegacyHeaderSetIsIdentityFree:
    """`legacy` must send exactly what it sent before the knob existed. The gate
    is the CONSTRUCTION, not `config.cookie`, so a profile that resolves to
    legacy while carrying live identities cannot leak one under a v32
    signature — anti-block invariant (c) on a real identity."""

    def _legacy_headers(self) -> dict[str, str]:
        signer = MetasecSigner(local_config(signer=SIGNER_LEGACY))
        _spying(signer)
        return signer.sign(url=URL, device_id='FAKE-DEV-1')

    def test_legacy_emits_exactly_the_six_base_keys(self):
        assert set(self._legacy_headers()) == BASE_KEYS

    def test_legacy_omits_every_identity_and_v46_header(self):
        headers = self._legacy_headers()
        assert not (set(headers) & V46_KEYS)

    def test_legacy_leaks_no_identity_material_in_any_value(self):
        values = ' '.join(self._legacy_headers().values())
        assert FAKE_COOKIE not in values
        assert FAKE_TOKEN not in values
        assert 'sessionid' not in values

    def test_legacy_never_uses_the_captured_user_agent(self):
        # The captured UA is identity material too: it names the warm device's
        # app build, which a v32 signature does not match.
        agent = self._legacy_headers()['User-Agent']
        assert agent != FAKE_UA
        assert agent.startswith(f'com.zhiliaoapp.musically/{V32_VERSION_CODE}')

    def test_legacy_sign_needs_no_iid(self):
        # Signature compatibility runs one way only: the cold path calls sign()
        # without `iid`, so a required-positional regression would break it.
        signer = MetasecSigner(ClientConfig(signer=SIGNER_LEGACY))
        _spying(signer)
        assert signer.sign(url=URL, device_id='FAKE-DEV-1')


class TestLocalHeaderSet:
    def _local_headers(self, **over) -> dict[str, str]:
        signer = MetasecSigner.for_v46(local_config(**over))
        _spying(signer)
        return _sign_local(signer)

    def test_local_adds_exactly_the_five_v46_keys(self):
        assert set(self._local_headers()) - BASE_KEYS == set(V46_KEYS)

    def test_local_sends_the_captured_identity_and_static_pair(self):
        headers = self._local_headers()
        assert headers['cookie'] == FAKE_COOKIE
        assert headers['x-tt-token'] == FAKE_TOKEN
        assert headers['x-tt-dm-status'] == DM_STATUS_LOGGED_IN
        assert headers['sdk-version'] == SDK_VERSION_HEADER
        assert headers['x-bd-kmsv'] == BD_KMSV_HEADER
        assert headers['User-Agent'] == FAKE_UA

    def test_local_without_any_identity_keeps_only_the_static_pair(self):
        headers = self._local_headers(cookie=None, x_tt_token=None)
        assert set(headers) - BASE_KEYS == {'sdk-version', 'x-bd-kmsv'}

    def test_cookie_alone_brings_the_dm_status_but_no_token_header(self):
        headers = self._local_headers(x_tt_token=None)
        assert headers['cookie'] == FAKE_COOKIE
        assert headers['x-tt-dm-status'] == DM_STATUS_LOGGED_IN
        assert 'x-tt-token' not in headers

    def test_token_alone_brings_no_cookie_and_no_dm_status(self):
        headers = self._local_headers(cookie=None)
        assert headers['x-tt-token'] == FAKE_TOKEN
        assert 'cookie' not in headers
        assert 'x-tt-dm-status' not in headers

    def test_local_falls_back_to_a_built_user_agent_without_a_captured_one(self):
        agent = self._local_headers(user_agent=None)['User-Agent']
        assert agent.startswith(f'com.zhiliaoapp.musically/{V32_VERSION_CODE}')


class TestSigningFailureSet:
    """`_SIGNING_FAILURES` decides what may trigger the PAID fallback, so its
    membership is a contract, not an implementation detail."""

    def test_exact_membership(self):
        assert _SIGNING_FAILURES == (MetasecBaseException, ProtoError, ValueError,
                                     struct.error, zlib.error)

    def test_type_error_is_excluded(self):
        # A changed `Metasec.sign` signature is a programming error; it must not
        # be able to buy a paid signature.
        assert not any(issubclass(TypeError, cls) for cls in _SIGNING_FAILURES)

    def test_a_type_error_from_the_signer_propagates_unconverted(self):
        signer = MetasecSigner.for_v46(local_config())
        _spying(signer, raises=TypeError("sign() got an unexpected keyword 'iid'"))
        with pytest.raises(TypeError):
            _sign_local(signer)

    def test_every_member_surfaces_as_a_transport_error(self):
        raised = (MetasecBaseException(LEAK), ProtoError(LEAK), ValueError(LEAK),
                  struct.error(LEAK), zlib.error(LEAK))
        for exc in raised:
            signer = MetasecSigner.for_v46(local_config())
            _spying(signer, raises=exc)
            with pytest.raises(TransportError) as excinfo:
                _sign_local(signer)
            assert type(exc).__name__ in str(excinfo.value)

    def test_the_transport_error_carries_only_the_exception_class_name(self):
        # `InvalidURL`'s own message embeds the full signed query string and
        # `InvalidEncryptionKey`'s embeds the sign key, so the original message
        # must not be re-emitted.
        for exc in (MetasecBaseException(LEAK), InvalidEncryptionKey(LEAK),
                    ProtoError(LEAK), ValueError(LEAK)):
            signer = MetasecSigner.for_v46(local_config())
            _spying(signer, raises=exc)
            with pytest.raises(TransportError) as excinfo:
                _sign_local(signer)
            assert LEAK not in str(excinfo.value)

    def test_the_transport_error_carries_no_config_value(self):
        signer = MetasecSigner.for_v46(local_config())
        _spying(signer, raises=ValueError(f'{FAKE_COOKIE} {FAKE_TOKEN} {URL}'))
        with pytest.raises(TransportError) as excinfo:
            _sign_local(signer)
        message = str(excinfo.value)
        for secret in (FAKE_COOKIE, FAKE_TOKEN, 'sessionid', 'keyword=ocean',
                       V46_APP_VERSION):
            assert secret not in message

    def test_the_original_exception_is_kept_as_the_cause(self):
        cause = ValueError('bad pad')
        signer = MetasecSigner.for_v46(local_config())
        _spying(signer, raises=cause)
        with pytest.raises(TransportError) as excinfo:
            _sign_local(signer)
        assert excinfo.value.__cause__ is cause


class TestRealSigningFailures:
    """The two failures the vendored code really raises, unstubbed."""

    def test_a_none_valued_sign_field_becomes_a_transport_error(self):
        # `from_mapping` filters config by KEY, never by value, so a bare
        # `sign_app_version:` in YAML lands as None and the vendored protobuf
        # encoder raises `ProtoError` — which is NOT a `MetasecBaseException`.
        # Unhandled it is an unmapped HTTP 500 with no 502 and no fallback.
        signer = MetasecSigner.for_v46(local_config(sign_app_version=None))
        with pytest.raises(TransportError) as excinfo:
            _sign_local(signer)
        assert ProtoError.__name__ in str(excinfo.value)

    def test_a_malformed_url_becomes_a_transport_error(self):
        signer = MetasecSigner.for_v46(local_config())
        with pytest.raises(TransportError):
            signer.sign(url=f'not-a-url?keyword={LEAK}', device_id='FAKE-DEV-1',
                        iid='FAKE-IID-1')

    def test_the_malformed_url_message_does_not_leak_the_query_string(self):
        # The real `InvalidURL` message is f'The URL - {url} is incorrect'.
        signer = MetasecSigner.for_v46(local_config())
        with pytest.raises(TransportError) as excinfo:
            signer.sign(url=f'not-a-url?keyword={LEAK}', device_id='FAKE-DEV-1',
                        iid='FAKE-IID-1')
        assert LEAK not in str(excinfo.value)
        assert 'not-a-url' not in str(excinfo.value)


class TestSignatureReplyIsChecked:
    def test_a_reply_missing_signature_keys_is_a_transport_error(self):
        # Unchecked this is a raw KeyError: an unmapped HTTP 500 instead of a
        # signing failure the fallback and the error map can both see.
        signer = MetasecSigner.for_v46(local_config())
        signer._metasec = type(
            'PartialMetasec', (), {'sign': lambda self, **kw: {'x-argus': 'A',
                                                               'x-gorgon': 'G'}}
        )()
        with pytest.raises(TransportError) as excinfo:
            _sign_local(signer)
        message = str(excinfo.value)
        assert 'x-ladon' in message and 'x-khronos' in message

    def test_all_four_signature_keys_are_required(self):
        for missing in SIGNATURE_KEYS:
            partial = {k: 'STUB' for k in SIGNATURE_KEYS if k != missing}
            signer = MetasecSigner.for_v46(local_config())
            signer._metasec = type(
                'PartialMetasec', (), {'sign': lambda self, _p=partial, **kw: dict(_p)}
            )()
            with pytest.raises(TransportError) as excinfo:
                _sign_local(signer)
            assert missing in str(excinfo.value)


class TestLocalNoUaWarning:
    """A `local` identity with no captured UA signs as v46 while its UA quotes
    the v32 `version_code` — a signer/app mismatch TikTok reads as hit_shark.
    It is a WARNING, not a raise: the pool builds an identity-free synthetic
    client when `identities.json` is absent, and that must not crash startup."""

    def test_warns_when_a_local_identity_has_no_captured_user_agent(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger=SIGNING_LOGGER):
            MetasecSigner.for_v46(local_config(user_agent=None))
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert 'no captured user_agent' in warnings[0].getMessage()

    def test_the_warning_names_the_device_and_no_secret(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger=SIGNING_LOGGER):
            MetasecSigner.for_v46(local_config(user_agent=None, device_id='FAKE-DEV-9'))
        message = caplog.records[0].getMessage()
        assert 'FAKE-DEV-9' in message
        for secret in (FAKE_COOKIE, FAKE_TOKEN, 'sessionid'):
            assert secret not in message

    def test_the_message_template_carries_no_secret_placeholder(self):
        lowered = LOCAL_NO_UA_MSG.lower()
        assert 'cookie' not in lowered
        assert 'x_tt_token' not in lowered
        assert 'sessionid' not in lowered

    def test_a_missing_user_agent_never_raises_and_still_signs(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger=SIGNING_LOGGER):
            signer = MetasecSigner.for_v46(
                ClientConfig(signer=SIGNER_LOCAL), launch_time=LAUNCH
            )
        assert all(signer.sign(url=URL, device_id='FAKE-DEV-1', iid='')[k]
                   for k in SIGNATURE_KEYS)

    def test_silent_when_the_captured_user_agent_is_present(
        self, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.WARNING, logger=SIGNING_LOGGER):
            MetasecSigner.for_v46(local_config())
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_legacy_construction_never_warns_about_a_user_agent(
        self, caplog: pytest.LogCaptureFixture
    ):
        # legacy never uses a captured UA, so its absence is not a problem.
        with caplog.at_level(logging.WARNING, logger=SIGNING_LOGGER):
            MetasecSigner(ClientConfig(signer=SIGNER_LEGACY))
            MetasecSigner(local_config(signer=SIGNER_LEGACY, user_agent=None))
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
