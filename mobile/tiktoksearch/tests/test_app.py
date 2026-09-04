"""Unit tests for the api/app.py startup-guard message contract.

Scope is deliberately narrow: the two startup banners must name the env var and
must never carry key material. No FastAPI app is created, no TestClient is used,
no ClientPool is built, and nothing here touches the network or identities.json.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.config import RAPIDAPI_KEY_ENV  # noqa: E402

MISSING_CONFIG_MSG = app_module.MISSING_CONFIG_MSG
NO_SIGNER_KEY_MSG = app_module.NO_SIGNER_KEY_MSG

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
