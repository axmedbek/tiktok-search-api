"""Unit tests for api/app.py startup: the guard-message contract, and which
signer a profile actually boots on.

Two layers, deliberately:

* the message/AST contract — the startup banners must name the env var, must be
  wired into `logger.error` with mode names and paths as their only arguments,
  and must never carry key material. No app is built for these.
* the startup MATRIX — `{local, rapid, legacy, unset} x {key, no key}` driven
  through the real `create_app` and its lifespan, asserting which banner fires
  and that no mode silently substitutes another. A keyless `signer: rapid` is
  refused outright, because `RapidSigner` raises on construction and the pool
  would take the process down before any banner was reached.

The matrix builds a pool of SYNTHETIC devices from a temp config. It never reads
`mobile/identities.json` (none exists beside a tmp_path config) and never
touches the network: conftest stubs the paid signer, and the local signer's
crypto runs in-process.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import ast
import inspect
import logging
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import AsgiClient, drive  # noqa: E402

from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.config import (  # noqa: E402
    RAPIDAPI_KEY_ENV,
    SIGNER_LEGACY,
    SIGNER_LOCAL,
    SIGNER_RAPID,
)

MISSING_CONFIG_MSG = app_module.MISSING_CONFIG_MSG
NO_SIGNER_KEY_MSG = app_module.NO_SIGNER_KEY_MSG
RAPID_WITHOUT_KEY_MSG = app_module.RAPID_WITHOUT_KEY_MSG
SIGNER_MODE_MSG = app_module.SIGNER_MODE_MSG
API_LOGGER = 'tiktoksearch.api'
# Key-shaped, and deliberately distinctive: no startup line may echo it.
FAKE_KEY = 'FAKE-KEY-NEVER-LOG-ME'
BANNER_MARK = 'No signer key configured'

# Attribute names that would put live credentials into a log line.
SECRET_ATTRS = ('rapidapi_key', 'cookie', 'x_tt_token', 'sessionid', 'device_query')

_MODULE_TREE = ast.parse(inspect.getsource(app_module))


def _logger_calls() -> list[ast.Call]:
    """Every `logger.<level>(...)` call in api/app.py."""
    out = []
    for node in ast.walk(_MODULE_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == 'logger'
        ):
            out.append(node)
    return out


class TestStartupGuardMessages:
    def test_both_rendered_messages_name_the_env_var(self):
        # Both templates carry the env-var NAME through a %s, so the contract is
        # on the rendered banner (the call-site args are pinned by the AST tests).
        assert RAPIDAPI_KEY_ENV in MISSING_CONFIG_MSG % ('cfg.yaml', RAPIDAPI_KEY_ENV)
        assert RAPIDAPI_KEY_ENV in NO_SIGNER_KEY_MSG % (
            RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV
        )

    def test_messages_carry_only_string_placeholders(self):
        for msg in (MISSING_CONFIG_MSG, NO_SIGNER_KEY_MSG):
            # No %(name)s / %r / %d smuggling a config object into the banner.
            assert msg.count('%') == msg.count('%s')

    def test_missing_config_message_renders_path_and_env_name_only(self):
        rendered = MISSING_CONFIG_MSG % ('/app/config/config_direct.yaml', RAPIDAPI_KEY_ENV)
        assert '/app/config/config_direct.yaml' in rendered
        assert RAPIDAPI_KEY_ENV in rendered
        assert '%s' not in rendered  # both placeholders consumed, no third one

    def test_no_signer_key_message_renders_env_name_only(self):
        rendered = NO_SIGNER_KEY_MSG % (RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV)
        assert rendered.count(RAPIDAPI_KEY_ENV) == 2
        assert '%s' not in rendered

    def test_no_signer_key_message_states_empty_is_config_not_hit_shark(self):
        # The whole point of the banner: stop the identity-vs-config
        # misdiagnosis that learned-lessons.md exists to prevent.
        assert 'hit_shark' in NO_SIGNER_KEY_MSG
        assert '.env' in NO_SIGNER_KEY_MSG

    def test_messages_contain_no_secret_field_names(self):
        for msg in (MISSING_CONFIG_MSG, NO_SIGNER_KEY_MSG):
            lowered = msg.lower()
            assert 'cookie' not in lowered
            assert 'x_tt_token' not in lowered
            assert 'sessionid' not in lowered


class TestLoggingNeverInterpolatesSecrets:
    def test_no_logger_call_interpolates_key_material(self):
        for call in _logger_calls():
            for arg in call.args + [kw.value for kw in call.keywords]:
                src = ast.unparse(arg)
                for attr in SECRET_ATTRS:
                    assert attr not in src, f'logger call leaks {attr}: {src}'

    def test_both_guard_messages_are_wired_into_logger_error(self):
        errors = [
            c for c in _logger_calls()
            if isinstance(c.func, ast.Attribute) and c.func.attr == 'error'
        ]
        by_msg = {ast.unparse(c.args[0]): [ast.unparse(a) for a in c.args[1:]]
                  for c in errors if c.args}
        assert 'MISSING_CONFIG_MSG' in by_msg
        assert 'NO_SIGNER_KEY_MSG' in by_msg
        # The only substituted values are the config path and the env-var NAME.
        assert by_msg['MISSING_CONFIG_MSG'] == ['config_path', 'RAPIDAPI_KEY_ENV']
        assert by_msg['NO_SIGNER_KEY_MSG'] == ['RAPIDAPI_KEY_ENV', 'RAPIDAPI_KEY_ENV']


class TestRapidWithoutKeyMessage:
    def test_it_names_the_mode_that_was_asked_for_and_the_env_var(self):
        rendered = RAPID_WITHOUT_KEY_MSG % (RAPIDAPI_KEY_ENV, RAPIDAPI_KEY_ENV)
        assert 'signer: rapid' in rendered
        assert rendered.count(RAPIDAPI_KEY_ENV) == 2
        assert '%s' not in rendered

    def test_it_offers_the_free_local_signer_as_the_alternative(self):
        assert 'signer: local' in RAPID_WITHOUT_KEY_MSG

    def test_it_contains_no_secret_field_names(self):
        lowered = RAPID_WITHOUT_KEY_MSG.lower()
        assert 'cookie' not in lowered
        assert 'x_tt_token' not in lowered
        assert 'sessionid' not in lowered

    def test_the_mode_banner_substitutes_mode_names_only(self):
        # Two %s, both mode names: the resolved mode and the raw `signer:` value.
        assert SIGNER_MODE_MSG.count('%') == 2
        assert SIGNER_MODE_MSG.count('%s') == 2
        rendered = SIGNER_MODE_MSG % (SIGNER_LOCAL, SIGNER_LOCAL)
        assert '%s' not in rendered


def _profile(**settings: object) -> str:
    """A minimal pool profile: one synthetic device, no identities file."""
    base: dict[str, object] = {'synthetic_devices': 1, 'acquire_timeout_s': 1,
                               'daily_request_cap_per_device': 10}
    base.update(settings)
    return '\n'.join(f'{key}: {value}' for key, value in base.items()) + '\n'


class TestStartupSignerMatrix:
    """`{local, rapid, legacy, unset} x {key, no key}`, booted for real."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A shell RAPIDAPI_KEY would silently move the keyless rows.
        monkeypatch.delenv(RAPIDAPI_KEY_ENV, raising=False)

    def _boot(self, tmp_path: Path, name: str, body: str,
              caplog: pytest.LogCaptureFixture) -> list[str]:
        """Run create_app + the lifespan that builds the pool; return what the
        api logger said."""
        path = tmp_path / name
        path.write_text(body, encoding='utf-8')
        with caplog.at_level(logging.INFO, logger=API_LOGGER):
            app = app_module.create_app(str(path))

            async def run() -> None:
                async with AsgiClient(app):
                    pass

            drive(run())
        return [r.getMessage() for r in caplog.records if r.name == API_LOGGER]

    def test_local_without_a_key_starts_silently(self, tmp_path: Path,
                                                 caplog: pytest.LogCaptureFixture):
        # Local signing is free; a missing RapidAPI key is normal, not an error.
        messages = self._boot(tmp_path, 'local.yaml',
                              _profile(signer=SIGNER_LOCAL), caplog)
        assert not [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_LOCAL, SIGNER_LOCAL) in messages

    def test_local_with_a_key_stays_local(self, tmp_path: Path,
                                          caplog: pytest.LogCaptureFixture):
        # A configured key is the narrow FALLBACK, not a mode change.
        messages = self._boot(
            tmp_path, 'local_key.yaml',
            _profile(signer=SIGNER_LOCAL, rapidapi_key=f"'{FAKE_KEY}'"), caplog)
        assert not [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_LOCAL, SIGNER_LOCAL) in messages

    def test_legacy_without_a_key_fires_the_banner(self, tmp_path: Path,
                                                   caplog: pytest.LogCaptureFixture):
        # The cold path returns empty BY DESIGN — the silent-empty
        # misconfiguration this banner exists for.
        messages = self._boot(tmp_path, 'legacy.yaml',
                              _profile(signer=SIGNER_LEGACY), caplog)
        assert [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_LEGACY, SIGNER_LEGACY) in messages

    def test_legacy_with_a_key_does_not_fire_the_banner(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        messages = self._boot(
            tmp_path, 'legacy_key.yaml',
            _profile(signer=SIGNER_LEGACY, rapidapi_key=f"'{FAKE_KEY}'"), caplog)
        assert not [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_LEGACY, SIGNER_LEGACY) in messages

    def test_unset_without_a_key_derives_legacy_and_fires_the_banner(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        messages = self._boot(tmp_path, 'unset.yaml', _profile(), caplog)
        assert [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_LEGACY, 'unset') in messages

    def test_unset_with_a_key_derives_rapid_and_is_silent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        messages = self._boot(tmp_path, 'unset_key.yaml',
                              _profile(rapidapi_key=f"'{FAKE_KEY}'"), caplog)
        assert not [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_RAPID, 'unset') in messages

    def test_rapid_with_a_key_is_silent(self, tmp_path: Path,
                                        caplog: pytest.LogCaptureFixture):
        messages = self._boot(
            tmp_path, 'rapid_key.yaml',
            _profile(signer=SIGNER_RAPID, rapidapi_key=f"'{FAKE_KEY}'"), caplog)
        assert not [m for m in messages if BANNER_MARK in m]
        assert SIGNER_MODE_MSG % (SIGNER_RAPID, SIGNER_RAPID) in messages

    def test_no_startup_line_echoes_the_key(self, tmp_path: Path,
                                            caplog: pytest.LogCaptureFixture):
        messages = self._boot(
            tmp_path, 'keyed.yaml',
            _profile(signer=SIGNER_LOCAL, rapidapi_key=f"'{FAKE_KEY}'"), caplog)
        assert messages
        assert not [m for m in messages if FAKE_KEY in m]


class TestKeylessRapidIsRefused:
    """`signer: rapid` is an explicit request for the paid signer. With no key
    it cannot sign a single request, and no other signer is substituted —
    `rapid` is the stale-sign-key diagnostic, so quietly running something else
    would destroy the only signal it exists to give."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RAPIDAPI_KEY_ENV, raising=False)

    def _config(self, tmp_path: Path) -> str:
        path = tmp_path / 'rapid_nokey.yaml'
        path.write_text(_profile(signer=SIGNER_RAPID), encoding='utf-8')
        return str(path)

    def test_create_app_raises_before_a_pool_is_built(self, tmp_path: Path):
        # Refused in create_app, not in the lifespan: ClientPool would raise a
        # bare ValueError out of RapidSigner's constructor first.
        with pytest.raises(ValueError):
            app_module.create_app(self._config(tmp_path))

    def test_the_refusal_names_the_mode_and_the_env_var(self, tmp_path: Path):
        with pytest.raises(ValueError) as excinfo:
            app_module.create_app(self._config(tmp_path))
        message = str(excinfo.value)
        assert 'signer: rapid' in message
        assert RAPIDAPI_KEY_ENV in message

    def test_the_refusal_carries_no_key_material(self, tmp_path: Path):
        with pytest.raises(ValueError) as excinfo:
            app_module.create_app(self._config(tmp_path))
        lowered = str(excinfo.value).lower()
        for secret in ('cookie', 'sessionid', 'x_tt_token'):
            assert secret not in lowered

    def test_an_env_key_satisfies_it(self, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch):
        # The env override is the sanctioned way to supply the key, so the
        # refusal must be about the RESOLVED key, not about the YAML.
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FAKE_KEY)
        assert app_module.create_app(self._config(tmp_path)) is not None
