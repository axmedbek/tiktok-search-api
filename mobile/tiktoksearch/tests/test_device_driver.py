"""Unit tests for `device/driver.py` — the app-driven harvest.

**NOTHING HERE TOUCHES WAYDROID, MITMPROXY OR A NETWORK.** Four seams make that
structurally impossible and every test below uses them:

* `DeviceDriver(run=...)` — the intent invocation. Every test passes a recorder;
  `subprocess.run` itself is only ever reached through a monkeypatch in
  `TestRunIntent`, which asserts on the kwargs and never lets a real process
  start.
* `DeviceDriver(clock=...)` / `(sleep=...)` — the clock and the wait. A test
  drives time by hand, so a 45-second timeout takes microseconds.
* `DeviceDriver(spool=...)` — the spool. Some tests pass a fake to observe the
  ORDER of the calls; the ones about staleness use the REAL `FileSpool` over
  `tmp_path`, because the guarantee is about files.

Conftest's session-wide `_forbid_real_network` still stands over all of it.

What is pinned, and why each one has a mode where a weaker test proves nothing:

| rule | the weaker test that would pass anyway |
|---|---|
| the census is taken BEFORE the intent | asserting `exclude` is non-empty — it is non-empty either way |
| a pre-existing entry is never served | a spool with only ONE entry (the pre-state already equals the post-state) |
| an entry stamped before the intent is rejected | a fake clock where fire time and stamp are equal |
| a timeout RAISES | asserting the return is falsy — a swallowed timeout returns an empty feed, which is also falsy |
| an unreadable body is not a timeout | catching `DeviceError` — both are `DeviceError` |
| an empty feed is a RESULT | asserting no records — a raise also produces no records |

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch.device import driver as driver_module  # noqa: E402
from tiktoksearch.device.driver import (  # noqa: E402
    DEFAULT_WAYLAND_DISPLAY,
    INTENT_ACTION,
    RUNTIME_DIR_TEMPLATE,
    WAYLAND_DISPLAY_KEY,
    XDG_RUNTIME_DIR_KEY,
    DeviceConfig,
    DeviceDriver,
    DeviceFeed,
    _poll_ceiling,
    intent_argv,
    profile_uri,
    run_intent,
    session_env,
    validated_user_id,
)
from tiktoksearch.device.errors import (  # noqa: E402
    DeviceError,
    HarvestTimeout,
    IntentFailed,
    UnreadableResponse,
    UnusableUserId,
)
from tiktoksearch.harvest_spool import FileSpool, SpoolEntry, write_entry  # noqa: E402

USER_ID = '7195575867517944837'
FAKE_BINARY = 'fake-waydroid'
# A value that would be a shell injection if anything on this path ever built a
# command STRING. Nothing does; this is the case that proves it.
SHELL_BAIT = '7195; rm -rf /'


def feed_body(ids, *, has_more=True, max_cursor=1_756_628_308_000) -> bytes:
    return json.dumps({'status_code': 0, 'has_more': has_more, 'max_cursor': max_cursor,
                       'aweme_list': [{'aweme_id': str(i), 'author': {'uid': USER_ID, 'unique_id': 'u'},
                                       'statistics': {}, 'create_time': 1_700_000_000 + i}
                                      for i in ids]}).encode('utf-8')


def entry(*, captured_at: float, ids=(1, 2), body: bytes | None = None) -> SpoolEntry:
    return SpoolEntry.from_response(user_id=USER_ID, captured_at=captured_at,
                                    body=feed_body(ids) if body is None else body)


class FakeClock:
    """A hand-driven wall clock. `advance` per read, so a poll loop moves."""

    def __init__(self, start: float = 1_000.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        value = self.now
        self.now += self.step
        return value


class FakeSpool:
    """Records the ORDER of its calls, which is what the census rule is about."""

    def __init__(self, names=(), entries=None, *, log=None) -> None:
        self._names = frozenset(names)
        self._entries = list(entries or [])
        self.log = [] if log is None else log
        self.since_calls: list[dict] = []

    def entry_names(self, user_id: str) -> frozenset[str]:
        self.log.append(('census', user_id))
        return self._names

    def newest_since(self, user_id: str, *, after: float, exclude=()) -> SpoolEntry | None:
        self.log.append(('poll', user_id))
        self.since_calls.append({'user_id': user_id, 'after': after, 'exclude': frozenset(exclude)})
        return self._entries.pop(0) if self._entries else None


def recorder(log=None, *, raises: BaseException | None = None):
    """An intent runner that records instead of spawning anything."""
    calls: list[dict] = []
    shared = [] if log is None else log

    def _run(argv, env, timeout_s):
        shared.append(('intent', argv[-1]))
        calls.append({'argv': list(argv), 'env': dict(env), 'timeout_s': timeout_s})
        if raises is not None:
            raise raises

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def config(tmp_path, **over) -> DeviceConfig:
    base = dict(spool_dir=str(tmp_path), waydroid_binary=FAKE_BINARY,
                harvest_timeout_s=2.0, poll_interval_s=0.5, intent_timeout_s=1.0)
    base.update(over)
    return DeviceConfig(**base)


class TestIntentArgv:
    def test_it_is_the_measured_invocation(self):
        assert intent_argv('waydroid', USER_ID) == [
            'waydroid', 'app', 'intent', INTENT_ACTION, f'snssdk1233://user/profile/{USER_ID}']

    def test_it_is_an_argv_list_and_never_a_command_string(self):
        argv = intent_argv('waydroid', USER_ID)
        assert isinstance(argv, list) and all(isinstance(part, str) for part in argv)
        # Every element is one argument, so no element is a shell fragment.
        assert not any(' ' in part for part in argv[:4])

    def test_the_uri_names_the_numeric_uid(self):
        assert profile_uri(USER_ID).endswith(f'/{USER_ID}')


class TestValidatedUserId:
    def test_a_decimal_uid_passes(self):
        assert validated_user_id(USER_ID) == USER_ID

    def test_surrounding_whitespace_is_stripped(self):
        assert validated_user_id(f'  {USER_ID}  ') == USER_ID

    @pytest.mark.parametrize('bad', ['', '   ', 'sirabasc', SHELL_BAIT, '719$(id)', '7195/../x',
                                     None, '9' * 33])
    def test_anything_that_is_not_a_uid_is_refused(self, bad):
        with pytest.raises(UnusableUserId):
            validated_user_id(bad)

    def test_the_refusal_is_transient_like_every_other_device_failure(self):
        # One classification for the whole module — see `device/errors.py`.
        assert issubclass(UnusableUserId, DeviceError)


class TestSessionEnv:
    """The CLI runs as the desktop user and needs its Wayland session. Both
    variables are READ when present and only DERIVED when absent — nothing is
    pinned to one machine."""

    def test_present_values_are_inherited_unchanged(self):
        env = session_env({WAYLAND_DISPLAY_KEY: 'wayland-7', XDG_RUNTIME_DIR_KEY: '/run/user/4242'})
        assert env[WAYLAND_DISPLAY_KEY] == 'wayland-7'
        assert env[XDG_RUNTIME_DIR_KEY] == '/run/user/4242'

    def test_absent_values_are_derived_from_the_uid(self):
        env = session_env({}, uid=1000)
        assert env[WAYLAND_DISPLAY_KEY] == DEFAULT_WAYLAND_DISPLAY
        assert env[XDG_RUNTIME_DIR_KEY] == RUNTIME_DIR_TEMPLATE.format(uid=1000)

    def test_the_runtime_dir_is_not_hardcoded_to_one_uid(self):
        assert session_env({}, uid=1000)[XDG_RUNTIME_DIR_KEY] != session_env({}, uid=1001)[XDG_RUNTIME_DIR_KEY]

    def test_the_rest_of_the_environment_is_carried_through(self):
        # The CLI needs PATH and HOME; a hand-built minimal environment is a
        # list that silently rots.
        env = session_env({'PATH': '/usr/bin', WAYLAND_DISPLAY_KEY: 'wayland-0',
                           XDG_RUNTIME_DIR_KEY: '/run/user/1000'})
        assert env['PATH'] == '/usr/bin'

    def test_no_environment_value_is_logged(self, caplog):
        caplog.set_level('DEBUG', logger='tiktoksearch.device.driver')
        session_env({'RABBITMQ_PASSWORD': 'NOT-A-REAL-PASSWORD'}, uid=1000)
        assert 'NOT-A-REAL-PASSWORD' not in caplog.text
        assert WAYLAND_DISPLAY_KEY in caplog.text, 'the derived KEY NAMES are the diagnostic'


class TestDeviceConfig:
    def test_it_is_frozen(self, tmp_path):
        with pytest.raises(Exception):
            config(tmp_path).harvest_timeout_s = 1.0

    @pytest.mark.parametrize('field', ['harvest_timeout_s', 'poll_interval_s', 'intent_timeout_s'])
    def test_a_non_positive_interval_is_refused_at_construction(self, tmp_path, field):
        # A zero poll interval is a busy loop, and it must not be discovered
        # at 3 a.m. in production.
        with pytest.raises(ValueError):
            config(tmp_path, **{field: 0.0})

    def test_a_missing_spool_dir_is_refused(self):
        with pytest.raises(ValueError):
            DeviceConfig(spool_dir='')


class TestTheHappyVisit:
    def test_it_fires_the_intent_and_returns_the_feed(self, tmp_path):
        run = recorder()
        spool = FakeSpool(entries=[entry(captured_at=1_000.0, ids=[1, 2, 3])])
        drv = DeviceDriver(config(tmp_path), spool=spool, run=run, clock=FakeClock(),
                           sleep=lambda _s: None, env={'X': '1'})
        feed = drv.fetch_posts(USER_ID)
        assert isinstance(feed, DeviceFeed)
        assert len(feed.aweme_list) == 3
        assert feed.has_more is True
        assert feed.max_cursor == 1_756_628_308_000
        assert feed.user_id == USER_ID
        assert len(run.calls) == 1
        assert run.calls[0]['argv'] == intent_argv(FAKE_BINARY, USER_ID)
        assert run.calls[0]['env'] == {'X': '1'}
        assert run.calls[0]['timeout_s'] == 1.0

    def test_an_empty_aweme_list_is_a_result_and_not_a_failure(self, tmp_path):
        # A LEGITIMATE empty: this account has no posts. The assertion is that
        # a feed OBJECT comes back — "no records" alone would also be true of
        # the raise this must not be.
        spool = FakeSpool(entries=[entry(captured_at=1_000.0, body=feed_body([], has_more=False))])
        drv = DeviceDriver(config(tmp_path), spool=spool, run=recorder(), clock=FakeClock(),
                           sleep=lambda _s: None)
        feed = drv.fetch_posts(USER_ID)
        assert isinstance(feed, DeviceFeed) and feed.aweme_list == () and feed.has_more is False

    def test_the_user_id_is_validated_before_any_process_is_started(self, tmp_path):
        run = recorder()
        drv = DeviceDriver(config(tmp_path), spool=FakeSpool(), run=run, clock=FakeClock(),
                           sleep=lambda _s: None)
        with pytest.raises(UnusableUserId):
            drv.fetch_posts(SHELL_BAIT)
        assert run.calls == [], 'nothing may be invoked with an unvalidated user_id'


class TestTheStaleEntryGuarantee:
    """The crux. An entry from a PREVIOUS visit must never be read as this
    visit's, or a job publishes a feed it did not fetch."""

    def test_the_census_is_taken_before_the_intent_is_fired(self, tmp_path):
        # ORDER, not content. Moving the census below the `_run` call is the
        # whole bug, and every content-shaped assertion passes with it moved.
        log: list = []
        spool = FakeSpool(names={'stale.json'}, entries=[entry(captured_at=1_000.0)], log=log)
        DeviceDriver(config(tmp_path), spool=spool, run=recorder(log), clock=FakeClock(),
                     sleep=lambda _s: None).fetch_posts(USER_ID)
        assert log[0] == ('census', USER_ID)
        assert log[1][0] == 'intent'
        assert log[2] == ('poll', USER_ID)

    def test_the_census_is_passed_to_every_poll_as_the_exclusion_set(self, tmp_path):
        names = {'a.json', 'b.json'}
        spool = FakeSpool(names=names, entries=[None, entry(captured_at=1_000.0)])
        DeviceDriver(config(tmp_path), spool=spool, run=recorder(), clock=FakeClock(step=0.1),
                     sleep=lambda _s: None).fetch_posts(USER_ID)
        assert spool.since_calls, 'the spool was never polled'
        assert all(call['exclude'] == frozenset(names) for call in spool.since_calls)

    def test_a_previous_visits_entry_is_never_served_even_though_it_is_the_newest(self, tmp_path):
        # The REAL FileSpool, because the guarantee is about files.
        #
        # The stale entry is stamped AFTER the fire time on purpose, so the
        # TIMESTAMP rule accepts it and the census is the only thing that can
        # reject it. Stamp it earlier and this test passes with the census
        # deleted, which is the no-op shape `.claude/rules/learned-lessons.md`
        # records: the pre-state would already equal the post-state.
        write_entry(str(tmp_path), entry(captured_at=1_005.0, ids=[99]))
        clock = FakeClock(start=1_000.0, step=0.4)
        drv = DeviceDriver(config(tmp_path), run=recorder(), clock=clock, sleep=lambda _s: None)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)
        # The second instrument would have waved it through: the entry is
        # there, is readable, and is newer than the visit.
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=1_000.0) is not None

    def test_this_visits_entry_is_served_while_the_previous_ones_are_not(self, tmp_path):
        write_entry(str(tmp_path), entry(captured_at=900.0, ids=[11]))
        write_entry(str(tmp_path), entry(captured_at=950.0, ids=[22]))
        fresh = [33, 44]

        def _run(argv, env, timeout_s):
            # The app answering, mid-wait: the addon writes a new entry.
            write_entry(str(tmp_path), entry(captured_at=1_000.5, ids=fresh))

        drv = DeviceDriver(config(tmp_path), run=_run, clock=FakeClock(start=1_000.0, step=0.1),
                           sleep=lambda _s: None)
        feed = drv.fetch_posts(USER_ID)
        assert [a['aweme_id'] for a in feed.aweme_list] == [str(i) for i in fresh]

    def test_an_entry_that_lands_between_the_census_and_the_intent_is_rejected(self, tmp_path):
        # The gap the census cannot see: a previous visit's response still in
        # flight. It is absent from the census, so only its timestamp can
        # reject it — which is why both instruments exist.
        def _run(argv, env, timeout_s):
            # Stamped BEFORE the fire time the driver read (1_000.0).
            write_entry(str(tmp_path), entry(captured_at=999.9, ids=[77]))

        drv = DeviceDriver(config(tmp_path), run=_run, clock=FakeClock(start=1_000.0, step=0.4),
                           sleep=lambda _s: None)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)

    def test_another_accounts_entry_is_not_this_visits_answer(self, tmp_path):
        other = SpoolEntry.from_response(user_id='7195575867517944838', captured_at=1_000.5,
                                         body=feed_body([55]))
        write_entry(str(tmp_path), other)
        drv = DeviceDriver(config(tmp_path), run=recorder(), clock=FakeClock(start=1_000.0, step=0.4),
                           sleep=lambda _s: None)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)


class TestTheBoundedWait:
    def test_a_timeout_raises_rather_than_returning_an_empty_feed(self, tmp_path):
        # RAISES. A swallowed timeout would return a feed with no records,
        # which is falsy and indistinguishable from a legitimate empty account
        # — and the broker would ack the job with nothing published.
        drv = DeviceDriver(config(tmp_path), spool=FakeSpool(), run=recorder(),
                           clock=FakeClock(start=1_000.0, step=0.4), sleep=lambda _s: None)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)

    def test_a_timeout_is_transient_so_the_job_requeues(self):
        assert issubclass(HarvestTimeout, DeviceError)

    def test_the_wait_sleeps_between_polls_rather_than_spinning(self, tmp_path):
        slept: list[float] = []
        drv = DeviceDriver(config(tmp_path, harvest_timeout_s=2.0, poll_interval_s=0.5),
                           spool=FakeSpool(), run=recorder(),
                           clock=FakeClock(start=1_000.0, step=0.5), sleep=slept.append)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)
        assert slept, 'the loop never yielded'
        assert all(0 <= value <= 0.5 for value in slept), slept

    def test_the_poll_count_is_bounded_when_the_clock_never_advances(self, tmp_path):
        # A wall clock is the only clock that can be compared with the spool
        # stamps another process wrote, and a wall clock can stand still or
        # step backwards. Without the poll ceiling the deadline alone would
        # never be reached and the wait would be unbounded.
        clock = FakeClock(start=1_000.0, step=0.0)
        spool = FakeSpool()
        drv = DeviceDriver(config(tmp_path, harvest_timeout_s=2.0, poll_interval_s=0.5),
                           spool=spool, run=recorder(), clock=clock, sleep=lambda _s: None)
        with pytest.raises(HarvestTimeout):
            drv.fetch_posts(USER_ID)
        assert len(spool.since_calls) == _poll_ceiling(2.0, 0.5)

    def test_the_poll_ceiling_never_fires_before_the_deadline_on_a_sane_clock(self):
        # The deadline stays the reason a wait ends in production; the ceiling
        # is only the guard that it ends at all.
        assert _poll_ceiling(45.0, 0.5) > 45.0 / 0.5

    def test_an_entry_that_arrives_after_a_few_polls_is_still_served(self, tmp_path):
        spool = FakeSpool(entries=[None, None, entry(captured_at=1_000.0)])
        drv = DeviceDriver(config(tmp_path), spool=spool, run=recorder(),
                           clock=FakeClock(start=1_000.0, step=0.1), sleep=lambda _s: None)
        assert len(drv.fetch_posts(USER_ID).aweme_list) == 2
        assert len(spool.since_calls) == 3


class TestAnUnreadableResponse:
    """A response that ARRIVED and could not be read. A different fact from a
    timeout, and the tests assert on the class — both are `DeviceError`, so
    `pytest.raises(DeviceError)` would pass for either."""

    @pytest.mark.parametrize('body', [b'', b'<html>nginx</html>', b'[1,2]',
                                      b'{"status_code": 0, "has_more": 0}'])
    def test_it_raises_unreadable_and_not_a_timeout(self, tmp_path, body):
        spool = FakeSpool(entries=[entry(captured_at=1_000.0, body=body)])
        drv = DeviceDriver(config(tmp_path), spool=spool, run=recorder(), clock=FakeClock(),
                           sleep=lambda _s: None)
        with pytest.raises(UnreadableResponse):
            drv.fetch_posts(USER_ID)

    def test_the_zero_byte_shape_is_not_reported_as_an_empty_feed(self, tmp_path):
        # HTTP 200, 0 bytes, `tt_orcas_res: 1` — the measured rejection shape.
        # `.claude/rules/anti-block.md`: never an empty success.
        spool = FakeSpool(entries=[entry(captured_at=1_000.0, body=b'')])
        drv = DeviceDriver(config(tmp_path), spool=spool, run=recorder(), clock=FakeClock(),
                           sleep=lambda _s: None)
        with pytest.raises(UnreadableResponse):
            drv.fetch_posts(USER_ID)

    def test_it_is_transient(self):
        assert issubclass(UnreadableResponse, DeviceError)


class TestRunIntent:
    """The one place `subprocess` is exercised, and it is monkeypatched: no
    process is ever started."""

    def test_it_never_uses_a_shell(self, monkeypatch):
        seen: dict = {}

        def _fake_run(argv, **kwargs):
            seen['argv'] = argv
            seen['kwargs'] = kwargs
            return subprocess.CompletedProcess(argv, 0, b'', b'')

        monkeypatch.setattr(driver_module.subprocess, 'run', _fake_run)
        run_intent(intent_argv(FAKE_BINARY, USER_ID), {'PATH': '/usr/bin'}, 5.0)
        assert seen['kwargs'].get('shell') in (None, False)
        assert isinstance(seen['argv'], list)
        assert seen['kwargs']['timeout'] == 5.0
        assert seen['kwargs']['env'] == {'PATH': '/usr/bin'}

    def test_a_non_zero_exit_is_an_intent_failure_naming_the_code(self, monkeypatch):
        monkeypatch.setattr(driver_module.subprocess, 'run',
                            lambda argv, **kw: subprocess.CompletedProcess(argv, 3, b'', b'no session\nline2'))
        with pytest.raises(IntentFailed) as caught:
            run_intent([FAKE_BINARY], {}, 1.0)
        assert 'exited 3' in str(caught.value)
        assert 'no session' in str(caught.value)
        assert 'line2' not in str(caught.value), 'only the first stderr line is quoted'

    def test_a_missing_binary_is_an_intent_failure(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise FileNotFoundError(2, 'No such file')

        monkeypatch.setattr(driver_module.subprocess, 'run', _raise)
        with pytest.raises(IntentFailed):
            run_intent([FAKE_BINARY], {}, 1.0)

    def test_a_hung_cli_is_an_intent_failure(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 1.0)

        monkeypatch.setattr(driver_module.subprocess, 'run', _raise)
        with pytest.raises(IntentFailed):
            run_intent([FAKE_BINARY], {}, 1.0)

    def test_an_os_error_reports_the_class_and_not_its_text(self, monkeypatch):
        def _raise(argv, **kwargs):
            raise PermissionError(13, 'secret-looking detail')

        monkeypatch.setattr(driver_module.subprocess, 'run', _raise)
        with pytest.raises(IntentFailed) as caught:
            run_intent([FAKE_BINARY], {}, 1.0)
        assert 'PermissionError' in str(caught.value)
        assert 'secret-looking detail' not in str(caught.value)

    def test_an_intent_failure_reaches_the_caller_of_fetch_posts(self, tmp_path):
        drv = DeviceDriver(config(tmp_path), spool=FakeSpool(),
                           run=recorder(raises=IntentFailed('container down')),
                           clock=FakeClock(), sleep=lambda _s: None)
        with pytest.raises(IntentFailed):
            drv.fetch_posts(USER_ID)

    def test_it_is_transient(self):
        assert issubclass(IntentFailed, DeviceError)


class TestNothingHardcodesTheDeveloperMachine:
    def test_no_container_address_appears_in_the_module(self):
        # The `waydroid` CLI addresses the container itself, so its IP is never
        # a value this code has to know.
        source = Path(driver_module.__file__).read_text(encoding='utf-8')
        code = '\n'.join(line for line in source.splitlines() if not line.strip().startswith('#'))
        assert '192.168.240' not in code

    def test_no_uid_literal_appears_in_the_derived_runtime_dir(self):
        assert '{uid}' in RUNTIME_DIR_TEMPLATE
