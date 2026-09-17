"""Handle-resolution regressions using real temporary spools and no processes/network."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_broker_consumer import (  # noqa: E402
    BACKOFFS,
    SETTINGS,
    SHORT,
    FakeApi,
    FakeChannel,
    FakeConnection,
    page_body,
)
from tiktoksearch.broker.consumer import PAGE_QUEUE, RESULT_ROUTING_KEY, BrokerConsumer  # noqa: E402
from tiktoksearch.broker.device_source import DevicePageSource  # noqa: E402
from tiktoksearch.device.driver import DeviceConfig, DeviceDriver  # noqa: E402
from tiktoksearch.device.errors import HarvestTimeout, ProfileUnavailable, UnreadableResponse  # noqa: E402
from tiktoksearch.harvest_spool import (  # noqa: E402
    PROFILE_PATH,
    UNIQUE_ID_PATH,
    FileSpool,
    ProfileEntry,
    ProfileSpool,
    SpoolEntry,
    write_entry,
    write_profile_entry,
)

HANDLE = 'available_test_user'
MISSING_HANDLE = 'missing_test_user'
USER_ID = '7195575867517944837'
START = 1_000.0
TIMEOUT = 3.0
NOT_FOUND_BODY = json.dumps({'status_code': 8196, 'status_msg': "Couldn't find this account"}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def resolver_entry(handle: str, *, at: float, body: bytes | None = NOT_FOUND_BODY) -> ProfileEntry | None:
    # None for anything but the confirmed 8196 rejection: an unknown or unreadable
    # resolver reply must NOT land under the handle, or the driver would read it as
    # the profile answer before the real profile reply arrives.
    url = f'https://api.tiktokv.com{UNIQUE_ID_PATH}?{urlencode({"id": handle})}'
    return ProfileEntry.from_resolver_response(url=url, captured_at=at, body=body)


def write_resolver(directory: Path, handle: str, *, at: float, body: bytes | None = NOT_FOUND_BODY) -> None:
    entry = resolver_entry(handle, at=at, body=body)
    if entry is not None:
        write_profile_entry(str(directory), entry)


def write_profile(directory: Path, clock: Clock, *, handle: str = HANDLE) -> None:
    body = {'status_code': 0, 'common': {
        'user_profile_info': {'uid': USER_ID, 'username': handle, 'sec_uid': 'fake-sec-uid'},
        'user_statics_info': {'aweme_count': 3},
    }}
    entry = ProfileEntry.from_response(url=f'https://api.tiktokv.com{PROFILE_PATH}',
                                       captured_at=clock(), body=json.dumps(body).encode())
    write_profile_entry(str(directory), entry)


def write_posts(directory: Path, clock: Clock, ids: tuple[int, ...]) -> None:
    body = {'status_code': 0, 'has_more': True, 'max_cursor': int(clock() * 1_000),
            'aweme_list': [{'aweme_id': str(post_id), 'create_time': 1_700_000_000 + post_id,
                            'author': {'uid': USER_ID, 'unique_id': HANDLE}, 'statistics': {}}
                           for post_id in ids]}
    entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=clock(), body=json.dumps(body).encode())
    write_entry(str(directory), entry)


def driver_for(directory: Path, clock: Clock, on_intent, *, sleep=None) -> DeviceDriver:
    config = DeviceConfig(spool_dir=str(directory), intent_backend='adb', adb_binary='fake-adb',
                          harvest_timeout_s=TIMEOUT, poll_interval_s=0.5,
                          intent_timeout_s=1.0, settle_s=1.0)
    return DeviceDriver(config, spool=FileSpool(str(directory)), profiles=ProfileSpool(str(directory)),
                        run=on_intent, clock=clock, sleep=clock.sleep if sleep is None else sleep, env={})


def test_resolver_not_found_finishes_without_waiting_for_a_profile(tmp_path):
    clock = Clock()

    def capture(argv, env, timeout_s):
        assert f'https://www.tiktok.com/@{MISSING_HANDLE}' in argv
        write_profile_entry(str(tmp_path), resolver_entry(MISSING_HANDLE, at=clock()))

    with pytest.raises(ProfileUnavailable) as caught:
        driver_for(tmp_path, clock, capture).visit_handle(MISSING_HANDLE, want=50)

    assert caught.value.status_code == 8196
    assert clock.sleeps == [], 'a known nonexistent account must not hold the queue for 45 seconds'


@pytest.mark.parametrize('body', [
    b'{"status_code":9999,"status_msg":"try again later"}',
    b'{"unexpected":"shape"}', b'not-json', None,
])
def test_unknown_or_malformed_resolver_reply_is_transient(tmp_path, body):
    clock = Clock()

    def capture(argv, env, timeout_s):
        write_resolver(tmp_path, HANDLE, at=clock(), body=body)

    # Nothing is spooled for the handle, so the visit waits for the real profile
    # reply and times out — transient, never a permanent rejection.
    with pytest.raises(HarvestTimeout):
        driver_for(tmp_path, clock, capture).visit_handle(HANDLE, want=50)

    assert clock.sleeps


def test_a_malformed_profile_response_is_not_a_permanent_account_rejection(tmp_path):
    clock = Clock()

    def capture(argv, env, timeout_s):
        url = f'https://api.tiktokv.com{PROFILE_PATH}?{urlencode({"sec_user_id": HANDLE})}'
        entry = ProfileEntry.from_response(url=url, captured_at=clock(), body=b'{"status_code":0}')
        write_profile_entry(str(tmp_path), entry)

    with pytest.raises(UnreadableResponse):
        driver_for(tmp_path, clock, capture).visit_handle(HANDLE, want=50)


def test_a_preexisting_rejection_cannot_override_a_fresh_valid_profile(tmp_path):
    clock = Clock()
    # The old response deliberately has a FUTURE timestamp: only the census
    # prevents it from winning the newest-entry selection over the fresh profile.
    write_profile_entry(str(tmp_path), resolver_entry(HANDLE, at=START + 10))

    def capture(argv, env, timeout_s):
        write_profile(tmp_path, clock)
        write_posts(tmp_path, clock, (1, 2, 3))

    profile, feed = driver_for(tmp_path, clock, capture).visit_handle(HANDLE, want=3)

    assert profile.user_id == USER_ID
    assert [post['aweme_id'] for post in feed.aweme_list] == ['1', '2', '3']


def test_a_late_written_rejection_stamped_before_this_visit_is_ignored(tmp_path):
    clock = Clock()

    def capture(argv, env, timeout_s):
        # Written after the census but captured during a previous visit.
        write_profile_entry(str(tmp_path), resolver_entry(HANDLE, at=START - 1))

    with pytest.raises(HarvestTimeout):
        driver_for(tmp_path, clock, capture).visit_handle(HANDLE, want=50)

    assert clock.now == START + TIMEOUT


def test_a_valid_profile_collects_later_post_pages_and_deduplicates(tmp_path):
    clock = Clock()
    appended = False

    def capture(argv, env, timeout_s):
        write_profile(tmp_path, clock)
        write_posts(tmp_path, clock, (1, 2))

    def sleep(seconds):
        nonlocal appended
        clock.sleep(seconds)
        if not appended:
            write_posts(tmp_path, clock, (2, 3))
            appended = True

    profile, feed = driver_for(tmp_path, clock, capture, sleep=sleep).visit_handle(HANDLE, want=50)

    assert profile.username == HANDLE
    assert [post['aweme_id'] for post in feed.aweme_list] == ['1', '2', '3']
    assert START < feed.captured_at < clock.now < START + TIMEOUT


def test_missing_page_is_acked_and_the_next_page_publishes_every_post(tmp_path):
    clock = Clock()
    visited = []

    def capture(argv, env, timeout_s):
        url = next(arg for arg in argv if arg.startswith('https://www.tiktok.com/@'))
        handle = url.rsplit('@', 1)[1]
        visited.append(handle)
        if handle == MISSING_HANDLE:
            write_profile_entry(str(tmp_path), resolver_entry(handle, at=clock()))
        else:
            write_profile(tmp_path, clock)
            write_posts(tmp_path, clock, (1, 2))
            write_posts(tmp_path, clock, (2, 3))

    api = FakeApi()
    source = DevicePageSource(api, driver_for(tmp_path, clock, capture))
    channel = FakeChannel([
        (PAGE_QUEUE, page_body(page_id=17, page_name=MISSING_HANDLE,
                              page_url=f'https://www.tiktok.com/@{MISSING_HANDLE}')),
        (PAGE_QUEUE, page_body(page_id=18, page_name=HANDLE,
                              page_url=f'https://www.tiktok.com/@{HANDLE}')),
    ])
    connection = FakeConnection(channel)
    BrokerConsumer(SETTINGS, api, config=BACKOFFS, connect=lambda settings: connection,
                   page_source=source).run()

    assert visited == [MISSING_HANDLE, HANDLE]
    assert channel.acked == [1, 2]
    assert channel.nacked == [] and connection.sleeps == []
    assert [body['id'] for body in channel.bodies] == ['3', '2', '1']
    assert all(body['metadata']['page_id'] == 18 for body in channel.bodies)
    assert all(body['search_type'] == 'page' for body in channel.bodies)
    assert all(message['routing_key'] == RESULT_ROUTING_KEY for message in channel.published)
    assert api.calls == [], 'device resolution must not spend a signed API search'


def test_unknown_resolver_error_is_requeued_instead_of_losing_the_job(tmp_path):
    clock = Clock()

    def capture(argv, env, timeout_s):
        body = b'{"status_code":9999,"status_msg":"temporary failure"}'
        write_resolver(tmp_path, HANDLE, at=clock(), body=body)

    api = FakeApi()
    source = DevicePageSource(api, driver_for(tmp_path, clock, capture))
    channel = FakeChannel([(PAGE_QUEUE, page_body(page_name=HANDLE,
                                                 page_url=f'https://www.tiktok.com/@{HANDLE}'))])
    connection = FakeConnection(channel)
    BrokerConsumer(SETTINGS, api, config=BACKOFFS, connect=lambda settings: connection,
                   page_source=source).run()

    # A harvest timeout is re-published at the tail of the page queue (never to the result
    # routing key) and the original delivery is acked, so one stuck handle cannot block the queue.
    assert channel.nacked == [] and channel.acked == [1]
    assert [m['routing_key'] for m in channel.published] == [PAGE_QUEUE]
    assert connection.sleeps == []
