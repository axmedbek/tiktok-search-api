"""Unit tests for config loading (no network, no identities file).

Two things:

* the `RAPIDAPI_KEY` environment override added by the Docker epic — the key is
  no longer committed in a YAML profile, so `from_mapping` must resolve it from
  the environment while leaving every other field and the frozen-dataclass
  contract untouched;
* the `signer:` knob and `resolved_signer()` — which signer (and therefore which
  request path) a profile runs. The derivation is a backwards-compat guarantee:
  a profile that never mentions `signer:` must behave exactly as it did before
  the knob existed, and a typo must be refused at the YAML boundary rather than
  deriving silently onto the paid signer.

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
    SIGNER_LEGACY,
    SIGNER_LOCAL,
    SIGNER_MODES,
    SIGNER_RAPID,
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


class TestResolvedSignerExplicit:
    """An explicit `signer:` decides the mode outright."""

    def test_each_mode_wins_over_the_derivation(self):
        # `rapid`/`legacy` are asserted against the key state that would have
        # DERIVED the other one, so a regression to the old
        # `bool(rapidapi_key)` switch cannot pass.
        assert ClientConfig(signer='local').resolved_signer() == SIGNER_LOCAL
        assert ClientConfig(signer='rapid').resolved_signer() == SIGNER_RAPID
        assert ClientConfig(
            signer='legacy', rapidapi_key=FROM_YAML
        ).resolved_signer() == SIGNER_LEGACY

    def test_case_and_surrounding_whitespace_are_normalised(self):
        raw = ('  LOCAL ', 'Rapid', '\tLEGACY\n', 'LoCaL')
        assert [ClientConfig(signer=s).resolved_signer() for s in raw] == [
            SIGNER_LOCAL, SIGNER_RAPID, SIGNER_LEGACY, SIGNER_LOCAL
        ]

    def test_explicit_local_is_not_overridden_by_the_env_key(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The env override supplies the FALLBACK key; it must not silently move
        # a profile onto the paid signer.
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        cfg = ClientConfig.from_mapping({'signer': 'local'})
        assert cfg.rapidapi_key == FROM_ENV
        assert cfg.resolved_signer() == SIGNER_LOCAL

    def test_every_mode_name_resolves_to_itself(self):
        for mode in SIGNER_MODES:
            assert ClientConfig(signer=mode).resolved_signer() == mode


class TestResolvedSignerDerivation:
    """Unset derives `rapid` if a key is configured, else `legacy` — the
    backwards-compat guarantee for every profile that predates the knob."""

    def test_unset_with_a_key_derives_rapid(self):
        assert ClientConfig(rapidapi_key=FROM_YAML).resolved_signer() == SIGNER_RAPID

    def test_unset_without_a_key_derives_legacy(self):
        assert ClientConfig().resolved_signer() == SIGNER_LEGACY

    def test_none_signer_is_unset(self):
        # A bare `signer:` line in YAML parses as None.
        assert ClientConfig(signer=None).resolved_signer() == SIGNER_LEGACY
        assert ClientConfig(
            signer=None, rapidapi_key=FROM_YAML
        ).resolved_signer() == SIGNER_RAPID

    def test_whitespace_only_signer_is_unset(self):
        assert ClientConfig(signer='   ').resolved_signer() == SIGNER_LEGACY
        assert ClientConfig(
            signer='  \t ', rapidapi_key=FROM_YAML
        ).resolved_signer() == SIGNER_RAPID

    def test_from_mapping_stores_unset_as_empty_string(self):
        for raw in ({'signer': None}, {}, {'signer': '   '}):
            cfg = ClientConfig.from_mapping({**raw, 'rapidapi_key': FROM_YAML})
            assert cfg.signer == ''
            assert cfg.resolved_signer() == SIGNER_RAPID

    def test_from_mapping_stores_the_normalised_mode(self):
        assert ClientConfig.from_mapping({'signer': '  LoCaL  '}).signer == 'local'


class TestSignerRejectedAtTheYamlBoundary:
    """`resolved_signer()` DERIVES on anything it does not recognise, so a typo
    has to be refused where YAML enters — otherwise `signer: locl` on a keyed
    profile resolves to `rapid` and spends money on every request."""

    def test_unknown_non_empty_value_raises(self):
        for raw in ('locl', 'LOCAL_', 'rapidapi', 'x'):
            with pytest.raises(ValueError):
                ClientConfig.from_mapping({'signer': raw, 'rapidapi_key': FROM_YAML})

    def test_rejection_names_the_offending_value_and_the_valid_modes(self):
        with pytest.raises(ValueError) as excinfo:
            ClientConfig.from_mapping({'signer': 'locl'})
        message = str(excinfo.value)
        assert 'locl' in message
        for mode in SIGNER_MODES:
            assert mode in message

    def test_rejection_happens_through_pool_from_mapping_too(self):
        with pytest.raises(ValueError):
            PoolConfig.from_mapping({'signer': 'rapidapi', 'synthetic_devices': 1})

    def test_a_directly_constructed_unknown_value_derives_rather_than_crashing(self):
        # from_mapping is the only boundary; a hand-built config still resolves.
        assert ClientConfig(signer='locl').resolved_signer() == SIGNER_LEGACY
        assert ClientConfig(
            signer='locl', rapidapi_key=FROM_YAML
        ).resolved_signer() == SIGNER_RAPID


class TestResolvedSignerIsPure:
    def test_resolution_mutates_nothing(self):
        cfg = ClientConfig(signer='  LOCAL ', rapidapi_key=FROM_YAML)
        before = dataclasses.asdict(cfg)
        assert cfg.resolved_signer() == SIGNER_LOCAL
        assert cfg.resolved_signer() == SIGNER_LOCAL          # idempotent
        assert dataclasses.asdict(cfg) == before
        assert cfg.signer == '  LOCAL '                       # not normalised in place

    def test_config_is_still_frozen_after_resolution(self):
        cfg = ClientConfig(signer='local')
        cfg.resolved_signer()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.signer = SIGNER_RAPID


class TestCommittedProfilesResolveAsDocumented:
    """The three shipped profiles, loaded for real. This is what the
    unset-derivation exists to protect: `config_signed.yaml` must stay on the
    cold legacy path and `config_working.yaml` on the paid one, neither having
    ever heard of `signer:`. Only the PRESENCE of a key is ever asserted."""

    PROFILES = Path(__file__).resolve().parents[2]

    def _defaults(self, name: str) -> ClientConfig:
        return PoolConfig.load_yaml(str(self.PROFILES / name)).client_defaults

    def test_config_signed_has_no_knob_and_stays_legacy(self):
        cfg = self._defaults('config_signed.yaml')
        assert not cfg.signer
        # bool(), never the value: a failure here must not print a live key.
        assert bool(cfg.rapidapi_key) is False
        assert cfg.resolved_signer() == SIGNER_LEGACY

    def test_config_working_has_no_knob_and_stays_rapid(self):
        cfg = self._defaults('config_working.yaml')
        assert not cfg.signer
        assert bool(cfg.rapidapi_key) is True
        assert cfg.resolved_signer() == SIGNER_RAPID

    def test_config_direct_selects_local(self):
        cfg = self._defaults('config_direct.yaml')
        assert cfg.resolved_signer() == SIGNER_LOCAL

    def test_config_direct_stays_local_with_the_env_key_set(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(RAPIDAPI_KEY_ENV, FROM_ENV)
        assert self._defaults('config_direct.yaml').resolved_signer() == SIGNER_LOCAL
