"""The environment edge: broker credentials from the environment or `.env`.

A minimal stdlib `.env` reader rather than `python-dotenv`, per the plan: the
whole job is "split on the first `=`", and a dependency is an architectural
decision (`.claude/rules/dependencies.md`).

Nothing here is ever logged as a value. `.env` holds the live broker password;
the only diagnostics this module emits name KEYS and where they were resolved
from.
"""
from __future__ import annotations
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BrokerConfigError

logger = logging.getLogger('tiktoksearch.broker.env')

# The repo-root `.env`, resolved from THIS FILE and not from the process cwd.
# Same lesson as `api/app.py._resolve_identities_path`: a relative path
# resolved against the cwd made the server silently miss `identities.json`
# when it was launched from the repo root.
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[3] / '.env'

HOST_ENV = 'RABBITMQ_HOST'
PORT_ENV = 'RABBITMQ_PORT'
VHOST_ENV = 'RABBITMQ_VHOST'
USER_ENV = 'RABBITMQ_USER'
PASSWORD_ENV = 'RABBITMQ_PASSWORD'
EXCHANGE_ENV = 'RABBITMQ_EXCHANGE'

# Defaults for the three settings that have a conventional or measured value.
# `host`, `user` and `password` have none and are REQUIRED: guessing
# `localhost`/`guest` would turn a missing credential into a confusing
# connection refusal instead of a clear start-up error.
DEFAULT_PORT = 5672
DEFAULT_VHOST = '/'
# Measured on the live broker: exchange `sm.scraping.tiktok`, type direct,
# durable. Overridable via the env key because it is deployment data, not a
# law of this codebase.
DEFAULT_EXCHANGE = 'sm.scraping.tiktok'

_COMMENT_PREFIX = '#'
_EXPORT_PREFIX = 'export '
_ASSIGNMENT = '='
_QUOTES = ('"', "'")


@dataclass(frozen=True, slots=True)
class BrokerSettings:
    """Everything needed to open the AMQP connection.

    Frozen, per `.claude/rules/code-standards.md`. `password` carries
    `repr=False` on purpose: a dataclass `__repr__` prints every field, so the
    default would put the live broker password into any log line, traceback or
    debugger frame that rendered this object."""
    host: str
    port: int
    vhost: str
    user: str
    password: str = field(repr=False)
    exchange: str


def parse_env(text: str) -> dict[str, str]:
    """Parse `.env` text into a mapping. Never logs a value.

    Blank lines and `#` comment lines are skipped, a leading `export ` is
    tolerated, and the split is on the FIRST `=` so a value may contain more.
    A trailing `# ...` is NOT stripped from a value: an unquoted password may
    legitimately contain `#`, and silently truncating it would produce an
    authentication failure with no visible cause. Surrounding single or double
    quotes are removed."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIX):
            continue
        if line.startswith(_EXPORT_PREFIX):
            line = line[len(_EXPORT_PREFIX):].lstrip()
        key, separator, value = line.partition(_ASSIGNMENT)
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        values[key] = _unquote(value.strip())
    return values


def read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse an env file, or return `{}` when it does not exist.

    A missing file is normal — every key may come from the real environment.
    An UNREADABLE file is not: it is reported rather than silently treated as
    empty, which would surface as "missing credentials" and send the operator
    looking in the wrong place."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        logger.debug('no env file at %s; using the process environment only', path)
        return {}
    except OSError as exc:
        raise BrokerConfigError(f'cannot read env file {path}: {exc.strerror}') from exc
    return parse_env(text)


def broker_settings(*, env_path: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None) -> BrokerSettings:
    """Resolve the broker settings; the environment wins over the file.

    That precedence is what lets an operator override one key for one run
    without editing the file that holds the password."""
    path = DEFAULT_ENV_PATH if env_path is None else env_path
    from_file = read_env_file(path)
    from_environ = os.environ if environ is None else environ

    def value(key: str) -> str | None:
        found = from_environ.get(key) or from_file.get(key)
        return found.strip() or None if found is not None else None

    missing = [key for key in (HOST_ENV, USER_ENV, PASSWORD_ENV) if value(key) is None]
    if missing:
        raise BrokerConfigError(f"missing broker credentials: {', '.join(missing)} — set them in the environment or in {path}")
    # Only KEY NAMES and non-secret settings are logged, never a value: this
    # runs one line after the password was read.
    logger.debug('broker settings resolved from %s and the environment', path)
    return BrokerSettings(host=_require(value(HOST_ENV), HOST_ENV), port=_port(value(PORT_ENV)), vhost=value(VHOST_ENV) or DEFAULT_VHOST, user=_require(value(USER_ENV), USER_ENV), password=_require(value(PASSWORD_ENV), PASSWORD_ENV), exchange=value(EXCHANGE_ENV) or DEFAULT_EXCHANGE)


def _require(value: str | None, key: str) -> str:
    # Unreachable while `broker_settings` checks the same three keys first;
    # kept so the type is `str` and not `str | None` without an assert, and so
    # a fourth required key added to one list and not the other fails loudly
    # instead of constructing a `BrokerSettings` with a None in it.
    if value is None:
        raise BrokerConfigError(f'missing broker credential: {key}')
    return value


def _port(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        # The KEY, not the value: a mis-set port is still operator data.
        raise BrokerConfigError(f'{PORT_ENV} is not an integer') from exc
    if not 1 <= port <= 65535:
        raise BrokerConfigError(f'{PORT_ENV} is outside the valid port range')
    return port


def _unquote(value: str) -> str:
    for quote in _QUOTES:
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value
