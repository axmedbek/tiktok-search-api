"""Drive the genuine TikTok app: open a profile by intent, read what it fetched.

One visit, in order:

1. `waydroid app intent android.intent.action.VIEW snssdk1233://user/profile/<user_id>`
2. wait — BOUNDED — for a spool entry for that `user_id` belonging to THIS
   visit, written by `harvest_spool_addon.py` running under the mitmproxy that
   already decrypts the app's traffic;
3. return that entry's `aweme_list` with its `has_more` / `max_cursor`.

Why the app and not a signed request: measured 2026-09-10, the
`api32-core-alisg` gateway answers our local signer AND the paid RapidAPI
signer with HTTP 200, a 0-byte body and `tt_orcas_res: 1`, while the app's own
signature works and still worked replayed from plain `curl` 611 s later. Params,
headers, identity, HTTP version and TLS fingerprint were each ruled out by
measurement. So the app is the data source. A happy side effect: this path
issues no signed request, so `daily_request_cap_per_device` does not bind it.

### The stale-entry guarantee

The crux of this module. An entry from a PREVIOUS visit to the same profile
must never be read as this visit's, or a job publishes a feed it did not fetch.
Two independent instruments, and the driver applies both on every visit:

* **The census (primary, clock-free).** Before the intent is fired, the driver
  lists the entry file NAMES already present for this `user_id` and holds that
  set for the whole wait. An entry whose file existed before the visit is never
  eligible, whatever any clock says. This is what makes a completed previous
  visit structurally invisible — no timestamp comparison is trusted for it,
  because the addon and the driver are different processes and share only a
  wall clock.
* **The timestamp (secondary).** The entry's own `captured_at` must be at or
  after the moment the intent was fired. This closes the one gap the census
  cannot see: a previous visit's response still in flight that lands BETWEEN
  the census and the intent. The census took its snapshot before that file
  existed; the timestamp rejects it.

The residual case — a previous visit's response landing AFTER this intent was
fired — cannot be closed without a per-request nonce, and we cannot put one in
a request the app builds. It is bounded instead: the driver is serial (the
worker runs at `prefetch_count=1`, one job at a time), so a response can only
still be in flight from a visit that TIMED OUT, and a timeout requeues its job
without publishing. If such a late entry is then read by the next visit to the
same account, the data is still that account's feed from that endpoint, seconds
older. That is stated rather than hidden, and it is why `captured_at` is
recorded in every published-from entry.

### The wait is bounded

`harvest_timeout_s` is a deadline on the wall clock, and `_poll_ceiling` bounds
the number of polls independently — because the deadline is a WALL clock (it has
to be: the spool stamps are wall-clock, written by another process) and a wall
clock can step backwards, which would make a deadline alone unreachable. A
timeout raises `HarvestTimeout`, which is transient, which requeues the job.

Everything with a side effect is injectable — the spool, the intent invocation,
the clock and the sleep — so no unit test needs Waydroid, mitmproxy or TikTok
(`CLAUDE.md` golden rule 3).
"""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Collection, Mapping, Sequence

from ..harvest_spool import FileSpool, SpoolEntry
from .errors import DeviceError, HarvestTimeout, IntentFailed, UnreadableResponse, UnusableUserId

logger = logging.getLogger('tiktoksearch.device.driver')

# The Waydroid CLI, by name and not by path: it is installed on PATH and its
# location differs between a package install and a pip install.
WAYDROID_BINARY = 'waydroid'
# `waydroid app intent <action> <uri>` — the exact invocation measured to open
# a profile in the running app.
WAYDROID_INTENT_ARGS = ('app', 'intent')
INTENT_ACTION = 'android.intent.action.VIEW'
# TikTok's own deep-link scheme (`snssdk1233` is the musically/TikTok app id).
# The app resolves `user/profile/<uid>` against the NUMERIC uid, which is what
# `POST /profile` answers with.
PROFILE_URI_TEMPLATE = 'snssdk1233://user/profile/{user_id}'
# A TikTok uid is a decimal integer (19 digits today). Bounded and
# charset-checked before it becomes part of a URI — the same discipline
# `limits.USERNAME_PATTERN` applies to a handle that reaches a signed URL.
USER_ID_PATTERN = re.compile(r'^[0-9]{1,32}$')

# The app has to come to the foreground, resolve the deep link, and complete a
# signed request over the proxy. 45 s is generous for that and still far under
# RabbitMQ's default 30-minute `consumer_timeout`, which the backoff in
# `broker/consumer.py` also has to fit inside.
DEFAULT_HARVEST_TIMEOUT_S = 45.0
DEFAULT_POLL_INTERVAL_S = 0.5
# The CLI itself only has to hand an intent to the container.
DEFAULT_INTENT_TIMEOUT_S = 20.0

# The `waydroid` CLI talks to the session compositor, so it needs the desktop
# user's Wayland session in its environment. Both are READ from the process
# environment when present and only DERIVED when absent — nothing here pins the
# developer's machine. There is deliberately no container IP anywhere in this
# module either: the CLI addresses the container itself, so `192.168.240.112`
# is never a value this code has to know.
WAYLAND_DISPLAY_KEY = 'WAYLAND_DISPLAY'
XDG_RUNTIME_DIR_KEY = 'XDG_RUNTIME_DIR'
SESSION_ENV_KEYS = (WAYLAND_DISPLAY_KEY, XDG_RUNTIME_DIR_KEY)
# The conventional first Wayland socket, and the conventional per-uid runtime
# directory. Used ONLY when the variable is absent from the environment, e.g.
# when the worker was started from a systemd unit or a bare ssh session.
DEFAULT_WAYLAND_DISPLAY = 'wayland-0'
RUNTIME_DIR_TEMPLATE = '/run/user/{uid}'
# `waydroid`'s stderr is diagnostics, not data. Bounded because it lands in a
# log line and in an exception message.
MAX_LOGGED_STDERR_CHARS = 200


def session_env(environ: Mapping[str, str] | None = None, uid: int | None = None) -> dict[str, str]:
    """The environment `waydroid` is invoked with.

    The process environment, with `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR`
    filled in only where they are ABSENT. Inheriting the whole environment
    rather than building a minimal one is deliberate: the CLI needs `PATH`,
    `HOME` and its own Python's variables, and a hand-built environment is a
    list that silently rots.

    Nothing is logged from it. A process environment can hold anything,
    including this project's `RABBITMQ_PASSWORD`."""
    base = dict(os.environ if environ is None else environ)
    resolved_uid = os.getuid() if uid is None else uid
    derived = []
    if not base.get(WAYLAND_DISPLAY_KEY):
        base[WAYLAND_DISPLAY_KEY] = DEFAULT_WAYLAND_DISPLAY
        derived.append(WAYLAND_DISPLAY_KEY)
    if not base.get(XDG_RUNTIME_DIR_KEY):
        base[XDG_RUNTIME_DIR_KEY] = RUNTIME_DIR_TEMPLATE.format(uid=resolved_uid)
        derived.append(XDG_RUNTIME_DIR_KEY)
    if derived:
        # KEY NAMES only — never a value, and never the rest of the
        # environment. Which keys had to be derived is the diagnostic that
        # matters when the CLI cannot reach the compositor.
        logger.info('derived %s for the waydroid session (absent from the environment)',
                    ', '.join(derived))
    return base


def profile_uri(user_id: str) -> str:
    """The deep link that opens `user_id`'s profile in the app."""
    return PROFILE_URI_TEMPLATE.format(user_id=user_id)


def intent_argv(binary: str, user_id: str) -> list[str]:
    """The `waydroid app intent` command line, as an ARGV LIST.

    A list and never a string: nothing on this path is handed to a shell
    (`subprocess.run` is called without `shell=True`), so a `user_id` carrying
    a `;` or a `$(...)` is one argument and not a command. `user_id` is
    charset-checked by `validated_user_id` as well — belt and braces, because
    the value also becomes part of a URI the app parses."""
    return [binary, *WAYDROID_INTENT_ARGS, INTENT_ACTION, profile_uri(user_id)]


def validated_user_id(user_id: Any) -> str:
    """`user_id` as a TikTok uid, or `UnusableUserId`."""
    text = str(user_id).strip() if user_id is not None else ''
    if not USER_ID_PATTERN.match(text):
        # The VALUE is included: a uid is public (it is in the endpoint
        # contract and in every published message) and the operator cannot
        # diagnose a bad resolve without seeing it. Bounded by the pattern's
        # own failure, so a runaway string is truncated in the message.
        raise UnusableUserId(f'not a TikTok user_id: {text[:64]!r}')
    return text


def run_intent(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> None:
    """Fire one intent through the `waydroid` CLI. The driver's default runner.

    Injected into `DeviceDriver` as a callable so a unit test substitutes a
    recorder and no test ever spawns a process."""
    try:
        completed = subprocess.run(list(argv), env=dict(env), capture_output=True,
                                   timeout=timeout_s, check=False)
    except FileNotFoundError as exc:
        raise IntentFailed(f'{argv[0]} is not on PATH') from exc
    except subprocess.TimeoutExpired as exc:
        raise IntentFailed(f'{argv[0]} did not return within {timeout_s:g}s') from exc
    except OSError as exc:
        # The EXCEPTION CLASS, not its text: an OSError message can carry the
        # whole command line and the environment in play.
        raise IntentFailed(f'{argv[0]} could not be run ({type(exc).__name__})') from exc
    if completed.returncode != 0:
        raise IntentFailed(f'{argv[0]} exited {completed.returncode}: {_first_line(completed.stderr)}')


def _first_line(stream: bytes | None) -> str:
    if not stream:
        return '(no output)'
    text = stream.decode('utf-8', errors='replace').strip().splitlines()
    return text[0][:MAX_LOGGED_STDERR_CHARS] if text else '(no output)'


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    """Run-shape knobs for one device driver. Frozen per
    `.claude/rules/code-standards.md`; validated at construction, so an
    unusable interval cannot become a busy loop at 3 a.m."""

    spool_dir: str
    waydroid_binary: str = WAYDROID_BINARY
    harvest_timeout_s: float = DEFAULT_HARVEST_TIMEOUT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    intent_timeout_s: float = DEFAULT_INTENT_TIMEOUT_S

    def __post_init__(self) -> None:
        if not self.spool_dir:
            raise ValueError('spool_dir is required')
        for name in ('harvest_timeout_s', 'poll_interval_s', 'intent_timeout_s'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be greater than 0')


@dataclass(frozen=True, slots=True)
class DeviceFeed:
    """One visit's answer: the app's own post feed for one account.

    `aweme_list` holds RAW upstream awemes — the same shape
    `mapping.flatten_video` consumes on the signed path — so the caller
    produces records identical to the ones `/user/posts` publishes. Flattening
    is deliberately not done here: this module knows about the device, not
    about the outbound message contract.

    An EMPTY `aweme_list` is a legitimate answer (an account with no posts).
    It is not a timeout and not an unreadable body — both of those raise."""

    user_id: str
    aweme_list: tuple[Mapping[str, Any], ...]
    has_more: bool
    max_cursor: int | None
    captured_at: float


class DeviceDriver:
    """Open a profile in the app and return the feed it fetched."""

    def __init__(self, config: DeviceConfig, *, spool: Any | None = None,
                 run: Callable[[Sequence[str], Mapping[str, str], float], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self._config = config
        self._spool = FileSpool(config.spool_dir) if spool is None else spool
        self._run = run_intent if run is None else run
        # `time.time` and NOT `time.monotonic`: the entries this clock is
        # compared against are stamped by `harvest_spool_addon.py` in the
        # mitmdump process, and two processes share a wall clock and nothing
        # else. `_poll_ceiling` exists because of that choice.
        self._clock = time.time if clock is None else clock
        self._sleep = time.sleep if sleep is None else sleep
        self._env = session_env() if env is None else dict(env)

    @property
    def config(self) -> DeviceConfig:
        """The knobs this driver runs with."""
        return self._config

    def fetch_posts(self, user_id: str) -> DeviceFeed:
        """Visit `user_id`'s profile and return the feed the app fetched.

        Raises `UnusableUserId` for a value that is not a TikTok uid,
        `IntentFailed` when the CLI could not deliver the intent,
        `HarvestTimeout` when nothing arrived in time, and
        `UnreadableResponse` when something arrived that could not be read —
        every one of them a `DeviceError`, and therefore transient."""
        uid = validated_user_id(user_id)
        # THE CENSUS, and it is taken BEFORE the intent is fired. Moving this
        # line below the `_run` call is the whole stale-entry bug: a response
        # from a previous visit that is already on disk would then be absent
        # from `known`, pass the name filter, and be published as this visit's.
        known = self._spool.entry_names(uid)
        fired_at = self._clock()
        logger.info('visiting profile user_id=%s (%d spool entry/entries already present)',
                    uid, len(known))
        self._run(intent_argv(self._config.waydroid_binary, uid), self._env,
                  self._config.intent_timeout_s)
        return self._await_feed(uid, fired_at=fired_at, known=known)

    def _await_feed(self, user_id: str, *, fired_at: float, known: Collection[str]) -> DeviceFeed:
        timeout = self._config.harvest_timeout_s
        interval = self._config.poll_interval_s
        deadline = fired_at + timeout
        ceiling = _poll_ceiling(timeout, interval)
        polls = 0
        while True:
            entry = self._spool.newest_since(user_id, after=fired_at, exclude=known)
            if entry is not None:
                return _feed(user_id, entry, waited_s=max(self._clock() - fired_at, 0.0))
            polls += 1
            now = self._clock()
            if now >= deadline:
                # A NORMAL outcome. Raised, never softened into an empty feed:
                # "the app did not answer" and "this account has no posts" are
                # different facts, and only the second may be published.
                logger.warning('no spool entry for user_id=%s within %.0fs (%d poll(s), deadline reached)',
                               user_id, timeout, polls)
                raise HarvestTimeout(f'no post-feed response for user_id {user_id} within {timeout:g}s')
            if polls >= ceiling:
                # THE SECOND BOUND, and reaching it means the wall clock
                # misbehaved: `_poll_ceiling` carries slack, so on any clock
                # that advances the deadline above fires first. Same transient
                # outcome as the deadline — `HarvestTimeout`, so the broker
                # requeues the job instead of acking it with an empty feed —
                # but a DISTINCT log line, because "the app was slow" and "the
                # clock stood still or stepped backwards" are different
                # diagnoses and one message for both would hide the second.
                logger.warning('no spool entry for user_id=%s after %d poll(s) (poll ceiling reached: the '
                               'wall clock advanced %.1fs of the %.0fs deadline)',
                               user_id, polls, max(now - fired_at, 0.0), timeout)
                raise HarvestTimeout(f'no post-feed response for user_id {user_id} within {polls} poll(s)')
            self._sleep(min(interval, max(deadline - now, 0.0)))


def _poll_ceiling(timeout_s: float, interval_s: float) -> int:
    """How many polls a bounded wait may make.

    A second, clock-independent bound on the loop. The deadline is on the WALL
    clock — it must be, since the spool stamps are — and a wall clock that
    steps backwards (NTP, a suspend/resume) would make the deadline alone
    unreachable and the loop unbounded. Two extra polls of slack so this
    ceiling never fires BEFORE the deadline on a well-behaved clock: the
    deadline stays the reason a wait ends in production, and this stays the
    guard that it ends at all."""
    return int(math.ceil(timeout_s / interval_s)) + 2


def _feed(user_id: str, entry: SpoolEntry, *, waited_s: float) -> DeviceFeed:
    if not entry.is_ok:
        # A response DID arrive — the 0-byte `tt_orcas_res: 1` shape, an
        # undecodable body, or an object with no `aweme_list` list. Reporting
        # any of those as an empty success is what
        # `.claude/rules/anti-block.md` forbids.
        raise UnreadableResponse(f'post-feed response for user_id {user_id} was unreadable: {entry.reason}')
    logger.info('user_id=%s served %d aweme(s) after %.1fs (has_more=%s, max_cursor=%s)',
                user_id, len(entry.aweme_list), waited_s, entry.has_more, entry.max_cursor)
    return DeviceFeed(user_id=user_id, aweme_list=entry.aweme_list, has_more=entry.has_more,
                      max_cursor=entry.max_cursor, captured_at=entry.captured_at)


__all__ = ['DeviceConfig', 'DeviceDriver', 'DeviceError', 'DeviceFeed', 'HarvestTimeout',
           'IntentFailed', 'UnreadableResponse', 'UnusableUserId', 'intent_argv',
           'profile_uri', 'run_intent', 'session_env', 'validated_user_id']
