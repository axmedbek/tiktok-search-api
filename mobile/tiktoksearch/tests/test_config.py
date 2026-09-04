"""Unit tests for config loading (no network, no identities file).

Focus: the `RAPIDAPI_KEY` environment override added by the Docker epic — the key
is no longer committed in a YAML profile, so `from_mapping` must resolve it from
the environment while leaving every other field and the frozen-dataclass
contract untouched.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch.config import (  # noqa: E402
    RAPIDAPI_KEY_ENV,
    ClientConfig,
    PoolConfig,
)

FROM_YAML = 'FROM_YAML'
FROM_ENV = 'FROM_ENV'


def _mapping(**extra: object) -> dict[str, object]:
    """A minimal YAML-shaped mapping carrying a fake committed key."""
    base: dict[str, object] = {'rapidapi_key': FROM_YAML}
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may inherit or leak a real RAPIDAPI_KEY from the shell."""
    monkeypatch.delenv(RAPIDAPI_KEY_ENV, raising=False)


class TestRapidApiKeyEnvOverride:
    def test_env_key_wins_over_yaml_value(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        assert ClientConfig.from_mapping(_mapping()).rapidapi_key == FROM_ENV

    def test_env_key_applies_when_mapping_has_no_key_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        cfg = ClientConfig.from_mapping({'app_id': 1233})
        assert cfg.rapidapi_key == FROM_ENV

    def test_unset_env_falls_back_to_yaml_value(self):
        assert ClientConfig.from_mapping(_mapping()).rapidapi_key == FROM_YAML

    def test_empty_env_falls_back_to_yaml_value(self, monkeypatch: pytest.MonkeyPatch):
        # `docker compose up` with no .env expands ${RAPIDAPI_KEY:-} to ''.
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, '')
        assert ClientConfig.from_mapping(_mapping()).rapidapi_key == FROM_YAML

    def test_whitespace_only_env_falls_back_to_yaml_value(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Pins the .strip(): a .env line may carry trailing whitespace only.
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, '   ')
        assert ClientConfig.from_mapping(_mapping()).rapidapi_key == FROM_YAML

    def test_surrounding_whitespace_is_stripped_from_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, f'  {FROM_ENV}\n')
        assert ClientConfig.from_mapping(_mapping()).rapidapi_key == FROM_ENV

    def test_override_disturbs_no_other_field(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        cfg = ClientConfig.from_mapping(
            _mapping(
                app_id=9999,
                search_host='https://search-test.invalid',
                api_hosts=['https://h1.invalid', 'https://h2.invalid'],
            )
        )
        assert cfg.rapidapi_key == FROM_ENV
        assert cfg.app_id == 9999                                  # mapped
        assert cfg.search_host == 'https://search-test.invalid'    # mapped
        assert cfg.api_hosts == ('https://h1.invalid', 'https://h2.invalid')
        assert cfg.sign_app_version == '46.0.42'                   # default kept
        assert cfg.rapidapi_provider == 'tiktanic'                 # default kept
        assert cfg.rapidapi_host == 'tiktok-api-signer.p.rapidapi.com'

    def test_result_of_override_is_still_frozen(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        cfg = ClientConfig.from_mapping(_mapping())
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.rapidapi_key = 'MUTATED'

    def test_pool_from_mapping_propagates_env_key_to_client_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        pool = PoolConfig.from_mapping(_mapping(daily_request_cap_per_device=7))
        assert pool.client_defaults.rapidapi_key == FROM_ENV
        assert pool.daily_request_cap_per_device == 7


class TestRapidApiKeyEnvOverrideScope:
    """The override lives in `from_mapping` only — nothing else picks it up."""

    def test_bare_client_config_ignores_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        assert ClientConfig().rapidapi_key is None

    def test_bare_pool_config_ignores_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        assert PoolConfig().client_defaults.rapidapi_key is None

    def test_load_yaml_missing_path_ignores_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Known gap, pinned deliberately: load_yaml's missing-path branch returns
        # a bare cls() and bypasses from_mapping, so the env override does NOT
        # apply there. MISSING_CONFIG_MSG in api/app.py is what covers this.
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        missing = tmp_path / 'nope.yaml'
        assert PoolConfig.load_yaml(missing).client_defaults.rapidapi_key is None


class TestClientConfigWithOverrides:
    def test_per_device_key_wins_for_that_slot(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        base = ClientConfig.from_mapping(_mapping())
        slot = base.with_overrides({'rapidapi_key': 'FROM_DEVICE'})
        assert slot.rapidapi_key == 'FROM_DEVICE'
        assert base.rapidapi_key == FROM_ENV      # base untouched

    def test_empty_override_is_ignored(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        base = ClientConfig.from_mapping(_mapping())
        assert base.with_overrides({'rapidapi_key': ''}).rapidapi_key == FROM_ENV

    def test_none_override_is_ignored(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        base = ClientConfig.from_mapping(_mapping())
        assert base.with_overrides({'rapidapi_key': None}).rapidapi_key == FROM_ENV

    def test_unknown_override_key_dropped(self):
        base = ClientConfig.from_mapping(_mapping())
        slot = base.with_overrides({'not_a_field': 'x', 'device_id': 'dev-1'})
        assert slot.device_id == 'dev-1'
        assert not hasattr(slot, 'not_a_field')
