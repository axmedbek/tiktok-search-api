"""Drive the genuine TikTok app: open a profile by intent, read what it fetched.

One visit, in order:

1. fire the profile deep link `snssdk1233://user/profile/<user_id>` at the app,
   through one of two backends — the `waydroid app intent` CLI (Linux, the
   original), or `adb shell am start` (LDPlayer on Windows, measured 2026-09-14
   on Android 14 / TikTok 46.9.3);
2. wait — BOUNDED — for the spool entries for that `user_id` belonging to THIS
   visit, written by `harvest_spool_addon.py` running under the mitmproxy that
   already decrypts the app's traffic;
3. return their merged `aweme_list` with the newest entry's `has_more` /
   `max_cursor`.

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

### One visit is several entries

Measured 2026-09-14: opening a profile makes the app fetch TWO posts pages by
itself within ~12 s, without scrolling — first `count=9` (7–9 awemes), then
`count=18` (17–18 awemes). A job asks for up to 20, so the driver merges every
eligible entry of the visit (union in arrival order, deduplicated by
`aweme_id`) and stops on the FIRST of: enough unique awemes for `want`; no new
entry for `settle_s` seconds after the last one; the `harvest_timeout_s`
deadline (returning what was merged, if anything).

### The wait is bounded

`harvest_timeout_s` is a deadline on the wall clock, and `_poll_ceiling` bounds
the number of polls independently — because the deadline is a WALL clock (it has
to be: the spool stamps are wall-clock, written by another process) and a wall
clock can step backwards, which would make a deadline alone unreachable. A
timeout with nothing merged raises `HarvestTimeout`, which is transient, which
requeues the job.

Everything with a side effect is injectable — the spool, the intent invocation,
the clock and the sleep — so no unit test needs Waydroid, adb, mitmproxy or
TikTok (`CLAUDE.md` golden rule 3).
"""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Mapping, Sequence

from ..harvest_spool import FileSpool, ProfileEntry, ProfileSpool, SpoolEntry
from ..limits import MAX_USERNAME_CHARS, USERNAME_PATTERN
from .errors import (DeviceError, DeviceThrottled, HarvestTimeout, IntentFailed, ProfileUnavailable, UnreadableResponse,
                     UnusableHandle, UnusableUserId)

logger = logging.getLogger('tiktoksearch.device.driver')

# The two ways an intent reaches the app. `waydroid` is the Linux container's
# own CLI; `adb` addresses any emulator or phone exposing an adb port
# (LDPlayer on Windows listens on `127.0.0.1:5555`).
BACKEND_WAYDROID = 'waydroid'
BACKEND_ADB = 'adb'
INTENT_BACKENDS = (BACKEND_WAYDROID, BACKEND_ADB)
DEFAULT_INTENT_BACKEND = BACKEND_WAYDROID

# The Waydroid CLI, by name and not by path: it is installed on PATH and its
# location differs between a package install and a pip install.
WAYDROID_BINARY = 'waydroid'
# `waydroid app intent <action> <uri>` — the exact invocation measured to open
# a profile in the running app.
WAYDROID_INTENT_ARGS = ('app', 'intent')
# The adb client, by name for the same reason. `adb -s <serial> shell am start
# -a <action> -d <uri> <package>` is the measured invocation: `am start` exits
# 0 even when it could not resolve the intent, and reports that as a line
# starting with `Error:` instead — see `run_intent`.
ADB_BINARY = 'adb'
ADB_SERIAL_FLAG = '-s'
ADB_INTENT_ARGS = ('shell', 'am', 'start')
ADB_ACTION_FLAG = '-a'
ADB_DATA_FLAG = '-d'
# TikTok's package name, which pins the intent to the app rather than letting
# Android offer a chooser.
APP_PACKAGE = 'com.zhiliaoapp.musically'
# How `am start` reports an unresolvable intent on an exit code of 0.
ERROR_LINE_PREFIX = 'Error:'
INTENT_ACTION = 'android.intent.action.VIEW'
# TikTok's own deep-link scheme (`snssdk1233` is the musically/TikTok app id).
# The app resolves `user/profile/<uid>` against the NUMERIC uid, which is what
# `POST /profile` answers with.
PROFILE_URI_TEMPLATE = 'snssdk1233://user/profile/{user_id}'
# A TikTok uid is a decimal integer (19 digits today). Bounded and
# charset-checked before it becomes part of a URI — the same discipline
# `limits.USERNAME_PATTERN` applies to a handle that reaches a signed URL.
USER_ID_PATTERN = re.compile(r'^[0-9]{1,32}$')
# The profile's WEB URL. Opening it in the app (measured 2026-09-14, adb `am
# start -a VIEW -d <url> <package>`) makes the app resolve the HANDLE itself
# and call `/tiktok/user/profile/other/v1` — no user search, so a handle user
# search never surfaces (`@user12569217`) still resolves, and no search quota
# is spent. The handle is charset-checked against the API's own
# `limits.USERNAME_PATTERN` before it becomes part of this URL.
HANDLE_URL_TEMPLATE = 'https://www.tiktok.com/@{handle}'
HANDLE_PATTERN = re.compile(USERNAME_PATTERN)

# The app has to come to the foreground, resolve the deep link, and complete a
# signed request over the proxy. 45 s is generous for that and still far under
# RabbitMQ's default 30-minute `consumer_timeout`, which the backoff in
# `broker/consumer.py` also has to fit inside.
DEFAULT_HARVEST_TIMEOUT_S = 45.0
DEFAULT_POLL_INTERVAL_S = 0.5
# The CLI itself only has to hand an intent to the container.
DEFAULT_INTENT_TIMEOUT_S = 20.0
# How long after the last entry the visit is considered complete. The app's
# second page follows its first within a few seconds (measured: both inside
# ~12 s of the intent); 6 s is comfortably above that gap and well under the
# deadline.
DEFAULT_SETTLE_S = 6.0
# The field the merge deduplicates on: two pages of one visit may overlap.
AWEME_ID_KEY = 'aweme_id'

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
# A CLI's output is diagnostics, not data. Bounded because it lands in a log
# line and in an exception message.
MAX_LOGGED_OUTPUT_CHARS = 200


def session_env(environ: Mapping[str, str] | None = None, uid: int | None = None) -> dict[str, str]:
    """The environment `waydroid` is invoked with.

    The process environment, with `WAYLAND_DISPLAY` and `XDG_RUNTIME_DIR`
    filled in only where they are ABSENT. Inheriting the whole environment
    rather than building a minimal one is deliberate: the CLI needs `PATH`,
    `HOME` and its own Python's variables, and a hand-built environment is a
    list that silently rots.

    `XDG_RUNTIME_DIR` is derived from the uid, and the uid only exists on
    POSIX: `os.getuid` is absent on Windows, where no Wayland session exists
    either, so the default is simply not derived there rather than failing.

    Nothing is logged from it. A process environment can hold anything,
    including this project's `RABBITMQ_PASSWORD`."""
    base = dict(os.environ if environ is None else environ)
    resolved_uid = _process_uid() if uid is None else uid
    derived = []
    if not base.get(WAYLAND_DISPLAY_KEY):
        base[WAYLAND_DISPLAY_KEY] = DEFAULT_WAYLAND_DISPLAY
        derived.append(WAYLAND_DISPLAY_KEY)
    if not base.get(XDG_RUNTIME_DIR_KEY) and resolved_uid is not None:
        base[XDG_RUNTIME_DIR_KEY] = RUNTIME_DIR_TEMPLATE.format(uid=resolved_uid)
        derived.append(XDG_RUNTIME_DIR_KEY)
    if derived:
        # KEY NAMES only — never a value, and never the rest of the
        # environment. Which keys had to be derived is the diagnostic that
        # matters when the CLI cannot reach the compositor.
        logger.info('derived %s for the waydroid session (absent from the environment)',
                    ', '.join(derived))
    return base


def _process_uid() -> int | None:
    getuid = getattr(os, 'getuid', None)
    return getuid() if getuid is not None else None


def profile_uri(user_id: str) -> str:
    """The deep link that opens `user_id`'s profile in the app."""
    return PROFILE_URI_TEMPLATE.format(user_id=user_id)


def handle_url(handle: str) -> str:
    """The web URL that opens `handle`'s profile in the app."""
    return HANDLE_URL_TEMPLATE.format(handle=handle)


def intent_argv(binary: str, user_id: str) -> list[str]:
    """The `waydroid app intent` command line, as an ARGV LIST.

    A list and never a string: nothing on this path is handed to a shell
    (`subprocess.run` is called without `shell=True`), so a `user_id` carrying
    a `;` or a `$(...)` is one argument and not a command. `user_id` is
    charset-checked by `validated_user_id` as well — belt and braces, because
    the value also becomes part of a URI the app parses."""
    return _waydroid_argv(binary, profile_uri(user_id))


def adb_intent_argv(binary: str, user_id: str, *, serial: str | None = None,
                    package: str = APP_PACKAGE) -> list[str]:
    """The `adb shell am start` command line, as an ARGV LIST (see `intent_argv`).

    `-s <serial>` only when a serial is set: with one device attached adb
    needs none, and with several it refuses to guess."""
    return _adb_argv(binary, profile_uri(user_id), serial=serial, package=package)


def _waydroid_argv(binary: str, uri: str) -> list[str]:
    return [binary, *WAYDROID_INTENT_ARGS, INTENT_ACTION, uri]


def _adb_argv(binary: str, uri: str, *, serial: str | None, package: str) -> list[str]:
    target = [ADB_SERIAL_FLAG, serial] if serial else []
    return [binary, *target, *ADB_INTENT_ARGS, ADB_ACTION_FLAG, INTENT_ACTION, ADB_DATA_FLAG, uri, package]


def intent_argv_for(config: 'DeviceConfig', user_id: str) -> list[str]:
    """The intent command line for `config`'s backend, opening `user_id` by deep link."""
    return intent_argv_for_uri(config, profile_uri(user_id))


def intent_argv_for_uri(config: 'DeviceConfig', uri: str) -> list[str]:
    """The VIEW-intent command line for `config`'s backend and any `uri`."""
    if config.intent_backend == BACKEND_ADB:
        return _adb_argv(config.adb_binary, uri, serial=config.adb_serial, package=config.app_package)
    return _waydroid_argv(config.waydroid_binary, uri)


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


def validated_handle(handle: Any) -> str:
    """`handle` as a TikTok username (no leading `@`), or `UnusableHandle`."""
    text = str(handle).strip().removeprefix('@') if handle is not None else ''
    if not text or len(text) > MAX_USERNAME_CHARS or not HANDLE_PATTERN.match(text):
        raise UnusableHandle(f'not a TikTok handle: {text[:64]!r}')
    return text


def run_intent(argv: Sequence[str], env: Mapping[str, str], timeout_s: float) -> None:
    """Fire one intent through the backend CLI. The driver's default runner.

    Fails on a non-zero exit AND on an `Error:` line in stdout or stderr:
    `adb shell am start` exits 0 when it could not resolve the intent
    (measured: `Error: Activity not started, unable to resolve Intent ...`), so
    the exit code alone would report a visit the app never saw as fired.

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
    reported = _error_line(completed.stdout) or _error_line(completed.stderr)
    if reported is not None:
        raise IntentFailed(f'{argv[0]} exited 0 but reported: {reported}')


def _lines(stream: bytes | None) -> list[str]:
    if not stream:
        return []
    return stream.decode('utf-8', errors='replace').strip().splitlines()


def _first_line(stream: bytes | None) -> str:
    lines = _lines(stream)
    return lines[0][:MAX_LOGGED_OUTPUT_CHARS] if lines else '(no output)'


def _error_line(stream: bytes | None) -> str | None:
    for line in _lines(stream):
        if line.lstrip().startswith(ERROR_LINE_PREFIX):
            return line.strip()[:MAX_LOGGED_OUTPUT_CHARS]
    return None


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
    settle_s: float = DEFAULT_SETTLE_S
    intent_backend: str = DEFAULT_INTENT_BACKEND
    adb_binary: str = ADB_BINARY
    adb_serial: str | None = None
    app_package: str = APP_PACKAGE

    def __post_init__(self) -> None:
        if not self.spool_dir:
            raise ValueError('spool_dir is required')
        for name in ('harvest_timeout_s', 'poll_interval_s', 'intent_timeout_s', 'settle_s'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be greater than 0')
        if self.intent_backend not in INTENT_BACKENDS:
            raise ValueError(f'intent_backend must be one of {", ".join(INTENT_BACKENDS)}')


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


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """The account as the app's own profile reply stated it, for one visit.

    The fields of `harvest_spool.ProfileEntry` minus the spool bookkeeping —
    the envelope's `profile` dict and `/user/posts`' ids are built from this.
    `signature` is always None on this path (measured: the reply carries no
    bio text key), stated rather than hidden."""

    user_id: str
    sec_uid: str | None
    username: str | None
    nickname: str | None
    avatar_url: str | None
    follower_count: int | None
    following_count: int | None
    aweme_count: int | None
    heart_count: int | None
    signature: str | None
    verified: bool
    private: bool
    region_code: str | None
    captured_at: float

    @classmethod
    def from_entry(cls, entry: ProfileEntry) -> 'DeviceProfile':
        if not entry.user_id:
            raise ValueError('profile entry carries no user_id')
        return cls(user_id=entry.user_id, sec_uid=entry.sec_uid, username=entry.username,
                   nickname=entry.nickname, avatar_url=entry.avatar_url, follower_count=entry.follower_count,
                   following_count=entry.following_count, aweme_count=entry.aweme_count,
                   heart_count=entry.heart_count, signature=entry.signature, verified=entry.verified,
                   private=entry.private, region_code=entry.region_code, captured_at=entry.captured_at)


@dataclass(slots=True)
class _Visit:
    """What one visit has collected so far. Mutable, private to `_await_feed`."""

    names: set[str] = field(default_factory=set)
    awemes: list[Mapping[str, Any]] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    newest: SpoolEntry | None = None
    ok_entries: int = 0
    unreadable: SpoolEntry | None = None

    def absorb(self, name: str, entry: SpoolEntry) -> None:
        self.names.add(name)
        if not entry.is_ok:
            # Remembered, not raised: a readable page may still follow, and
            # only a visit with NO readable entry is an unreadable response.
            self.unreadable = entry
            return
        self.ok_entries += 1
        if self.newest is None or entry.captured_at >= self.newest.captured_at:
            self.newest = entry
        for aweme in entry.aweme_list:
            key = aweme.get(AWEME_ID_KEY)
            if key is not None:
                if str(key) in self.seen_ids:
                    continue
                self.seen_ids.add(str(key))
            self.awemes.append(aweme)


class DeviceDriver:
    """Open a profile in the app and return the feed it fetched."""

    def __init__(self, config: DeviceConfig, *, spool: Any | None = None, profiles: Any | None = None,
                 run: Callable[[Sequence[str], Mapping[str, str], float], None] | None = None,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], None] | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self._config = config
        self._spool = FileSpool(config.spool_dir) if spool is None else spool
        self._profiles = ProfileSpool(config.spool_dir) if profiles is None else profiles
        self._run = run_intent if run is None else run
        # `time.time` and NOT `time.monotonic`: the entries this clock is
        # compared against are stamped by `harvest_spool_addon.py` in the
        # mitmdump process, and two processes share a wall clock and nothing
        # else. `_poll_ceiling` exists because of that choice.
        self._clock = time.time if clock is None else clock
        self._sleep = time.sleep if sleep is None else sleep
        self._env = _default_env(config) if env is None else dict(env)

    @property
    def config(self) -> DeviceConfig:
        """The knobs this driver runs with."""
        return self._config

    def fetch_posts(self, user_id: str, *, want: int) -> DeviceFeed:
        """Visit `user_id`'s profile and return the feed the app fetched.

        `want` is how many posts the caller needs: the visit stops collecting
        as soon as that many unique awemes are merged, else when the app has
        gone quiet for `settle_s`, else at the deadline.

        Raises `UnusableUserId` for a value that is not a TikTok uid,
        `IntentFailed` when the CLI could not deliver the intent,
        `HarvestTimeout` when nothing arrived in time, and
        `UnreadableResponse` when only unreadable responses arrived —
        every one of them a `DeviceError`, and therefore transient."""
        uid = validated_user_id(user_id)
        # THE CENSUS, and it is taken BEFORE the intent is fired. Moving this
        # line below the `_run` call is the whole stale-entry bug: a response
        # from a previous visit that is already on disk would then be absent
        # from `known`, pass the name filter, and be published as this visit's.
        known = self._spool.entry_names(uid)
        fired_at = self._clock()
        logger.info('visiting profile user_id=%s via %s (%d spool entry/entries already present, want=%d)',
                    uid, self._config.intent_backend, len(known), want)
        self._run(intent_argv_for(self._config, uid), self._env, self._config.intent_timeout_s)
        return self._await_feed(uid, fired_at=fired_at, known=known, want=want)

    def visit_handle(self, handle: str, *, want: int) -> tuple[DeviceProfile, DeviceFeed]:
        """Open `handle`'s profile by its WEB URL; return the profile and the feed.

        ONE intent. The app resolves the handle itself, calls the profile
        endpoint (spooled under `profiles/`) and then fetches the two posts
        pages it always fetches on a profile open — so the posts are collected
        with `fetch_posts`' own loop, keyed by the uid the profile reply named,
        and no second intent is fired. No signed request, no search quota.

        BOTH censuses are taken before the intent: the profile census by
        handle, the posts census over the WHOLE posts spool, because the uid
        is not known until the profile arrives.

        Raises `UnusableHandle`, `IntentFailed`, `HarvestTimeout` (no profile
        reply, or no posts after it), `UnreadableResponse` as `fetch_posts`
        does, and `ProfileUnavailable` — the one non-transient outcome — when
        TikTok answered that the account is not there."""
        name = validated_handle(handle)
        known_profiles = self._profiles.entry_names(name)
        known_posts = self._spool.all_entry_names()
        fired_at = self._clock()
        logger.info('visiting profile handle=%s via %s (%d profile / %d posts entry/entries already present, want=%d)',
                    name, self._config.intent_backend, len(known_profiles), len(known_posts), want)
        self._run(intent_argv_for_uri(self._config, handle_url(name)), self._env, self._config.intent_timeout_s)
        entry = self._await_profile(name, fired_at=fired_at, known=known_profiles)
        if entry.is_throttled:
            logger.warning('resolver refused by risk control for handle=%s (0-byte reply): device throttled', name)
            raise DeviceThrottled(f'TikTok refused the handle resolver for {name} (risk control)')
        if entry.is_unavailable:
            logger.warning('profile for handle=%s unavailable (status_code=%s)', name, entry.status_code)
            raise ProfileUnavailable(f'TikTok has no profile for handle {name} (status_code={entry.status_code})',
                                     status_code=entry.status_code, status_msg=entry.status_msg)
        if not entry.is_ok:
            raise UnreadableResponse(f'TikTok profile response for handle {name} could not be read '
                                     f'(status_code={entry.status_code})')
        profile = DeviceProfile.from_entry(entry)
        if profile.aweme_count == 0 and not profile.private:
            # MEASURED 2026-09-15 (`@medianews_az`, aweme_count 0): the app's
            # post-feed request for an account with no posts answers "No more
            # videos" with NO `aweme_list` and, on this build, NO `user_id`
            # param either — so nothing can be spooled under the uid and the
            # wait is a guaranteed timeout. The profile already states the
            # answer: zero posts.
            logger.info('handle %s has no posts (user_id=%s, aweme_count=0): empty feed, no wait', name, profile.user_id)
            return profile, DeviceFeed(user_id=profile.user_id, aweme_list=(), has_more=False,
                                       max_cursor=None, captured_at=profile.captured_at)
        if profile.private:
            # MEASURED 2026-09-14 (`@hkimolu`): the app shows "This account is
            # private" and its post-feed request answers a 395-byte body with
            # no `aweme_list`, so nothing is ever spooled for the uid. Waiting
            # for it is a guaranteed timeout that requeued the job forever;
            # the honest answer is an EMPTY feed — a viewer who does not follow
            # the account sees no posts.
            logger.info('handle %s is a private account (user_id=%s): empty feed, no wait', name, profile.user_id)
            return profile, DeviceFeed(user_id=profile.user_id, aweme_list=(), has_more=False,
                                       max_cursor=None, captured_at=profile.captured_at)
        feed = self._await_feed(profile.user_id, fired_at=fired_at, known=known_posts, want=want)
        return profile, feed

    def _await_profile(self, handle: str, *, fired_at: float, known: Collection[str]) -> ProfileEntry:
        timeout = self._config.harvest_timeout_s
        deadline, ceiling = self._bounds(fired_at)
        polls = 0
        while True:
            entry = self._profiles.newest_since(handle, after=fired_at, exclude=known)
            polls += 1
            now = self._clock()
            if entry is not None:
                return entry
            if now >= deadline or polls >= ceiling:
                logger.warning('no profile entry for handle=%s within %.0fs (%d poll(s))', handle, timeout, polls)
                raise HarvestTimeout(f'no profile response for handle {handle} within {timeout:g}s')
            self._sleep(min(self._config.poll_interval_s, max(deadline - now, 0.0)))

    def _bounds(self, fired_at: float) -> tuple[float, int]:
        """`(deadline, poll ceiling)` for a wait that started at `fired_at`."""
        timeout = self._config.harvest_timeout_s
        return fired_at + timeout, _poll_ceiling(timeout, self._config.poll_interval_s)

    def _await_feed(self, user_id: str, *, fired_at: float, known: Collection[str], want: int) -> DeviceFeed:
        timeout = self._config.harvest_timeout_s
        interval = self._config.poll_interval_s
        deadline, ceiling = self._bounds(fired_at)
        polls = 0
        visit = _Visit()
        last_arrival: float | None = None
        while True:
            fresh = self._spool.entries_since(user_id, after=fired_at, exclude=frozenset(known) | visit.names)
            polls += 1
            now = self._clock()
            if fresh:
                for name, entry in fresh.items():
                    visit.absorb(name, entry)
                last_arrival = now
                if len(visit.awemes) >= want:
                    return self._finish(user_id, visit, fired_at=fired_at, why='enough')
            if last_arrival is not None and now - last_arrival >= self._config.settle_s:
                return self._finish(user_id, visit, fired_at=fired_at, why='settled')
            if now >= deadline:
                if visit.names:
                    return self._finish(user_id, visit, fired_at=fired_at, why='deadline')
                # A NORMAL outcome. Raised, never softened into an empty feed:
                # "the app did not answer" and "this account has no posts" are
                # different facts, and only the second may be published.
                logger.warning('no spool entry for user_id=%s within %.0fs (%d poll(s), deadline reached)',
                               user_id, timeout, polls)
                raise HarvestTimeout(f'no post-feed response for user_id {user_id} within {timeout:g}s')
            if polls >= ceiling:
                if visit.names:
                    return self._finish(user_id, visit, fired_at=fired_at, why='poll ceiling')
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

    def _finish(self, user_id: str, visit: _Visit, *, fired_at: float, why: str) -> DeviceFeed:
        waited_s = max(self._clock() - fired_at, 0.0)
        if visit.newest is None:
            # Every response of this visit ARRIVED and could not be read —
            # the 0-byte `tt_orcas_res: 1` shape, an undecodable body, or an
            # object with no `aweme_list` list. Reporting that as an empty
            # success is what `.claude/rules/anti-block.md` forbids.
            reason = visit.unreadable.reason if visit.unreadable is not None else None
            raise UnreadableResponse(f'post-feed response for user_id {user_id} was unreadable: {reason}')
        newest = visit.newest
        logger.info('user_id=%s served %d aweme(s) from %d entry/entries after %.1fs (%s; has_more=%s, max_cursor=%s)',
                    user_id, len(visit.awemes), visit.ok_entries, waited_s, why, newest.has_more, newest.max_cursor)
        return DeviceFeed(user_id=user_id, aweme_list=tuple(visit.awemes), has_more=newest.has_more,
                          max_cursor=newest.max_cursor, captured_at=newest.captured_at)


def _default_env(config: DeviceConfig) -> dict[str, str]:
    # Only the Waydroid CLI needs the Wayland session; adb inherits the
    # process environment untouched.
    if config.intent_backend == BACKEND_WAYDROID:
        return session_env()
    return dict(os.environ)


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


__all__ = ['DeviceConfig', 'DeviceDriver', 'DeviceError', 'DeviceFeed', 'DeviceProfile', 'HarvestTimeout',
           'IntentFailed', 'ProfileUnavailable', 'UnreadableResponse', 'UnusableHandle', 'UnusableUserId',
           'adb_intent_argv', 'handle_url', 'intent_argv', 'intent_argv_for', 'intent_argv_for_uri',
           'profile_uri', 'run_intent', 'session_env', 'validated_handle', 'validated_user_id']
