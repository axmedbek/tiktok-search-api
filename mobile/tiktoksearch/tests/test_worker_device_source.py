"""Unit tests for the device source and its wiring into the worker.

**NOTHING HERE TOUCHES THE BROKER, WAYDROID OR TIKTOK.** The seams are the same
ones `test_broker_consumer.py` established, and its fakes are IMPORTED rather
than re-declared — `FakeApi`, `FakeChannel` and `FakeConnection` are the
reviewed HTTP/pika stubs, and a second copy of them would be a second contract
to keep in step. Only the one-line `run_consumer` driver is local, because it
threads the new `page_source` seam through. That module's autouse
`_forbid_a_real_broker_connection` tripwire does not reach this one, so
`run_consumer` here always passes an injected `connect` and no code path can
reach `pika.BlockingConnection`.

Four rules carry the weight, and each has a weaker form that would pass while
the rule was broken:

| rule | the weaker test that would pass anyway |
|---|---|
| `--source` defaults to `search` | asserting the choice list contains `search` |
| `device` applies to PAGE jobs only | asserting a page job used the device (a source used for both does that too) |
| a device failure is TRANSIENT | asserting an `ApiCallError` was raised — a permanent one is also one |
| the envelope shape is unchanged | asserting the message has fields — so does a wrong one |

The `metadata.source_term` decision is pinned here too: there is no keyword on
this path, so the value is `device:<user_id>`, which is the convention
`client.POSTS_SOURCE_PREFIX` already established for this same endpoint reached
by a signed request (`posts:<user_id>`). No fake keyword is invented, and the
test asserts it cannot be mistaken for one.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
# `mobile/` is `parents[2]`, which also makes `worker.py` importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import worker as worker_module  # noqa: E402
from test_broker_consumer import (  # noqa: E402
    BACKOFFS,
    HANDLE,
    LONG,
    PROFILE_PAYLOAD,
    SETTINGS,
    SHORT,
    FakeApi,
    FakeChannel,
    FakeConnection,
    keyword_body,
    page_body,
    search_payload,
)
from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.broker.api_client import ApiCallError, Failure  # noqa: E402
from tiktoksearch.broker.consumer import KEYWORD_QUEUE, PAGE_QUEUE, BrokerConsumer, ConsumerConfig  # noqa: E402
from tiktoksearch.broker.device_source import (  # noqa: E402
    DEVICE_SOURCE_PREFIX,
    DevicePageSource,
    _newest_first,
    device_records,
)
from tiktoksearch.broker.envelope import body  # noqa: E402
from tiktoksearch.broker.errors import MalformedMessage  # noqa: E402
from tiktoksearch.broker.messages import PageMessage  # noqa: E402
from tiktoksearch.device.driver import DeviceConfig, DeviceFeed  # noqa: E402
from tiktoksearch.device.errors import (  # noqa: E402
    HarvestTimeout,
    IntentFailed,
    UnreadableResponse,
    UnusableUserId,
)
from tiktoksearch.mapping import flatten_video  # noqa: E402

USER_ID = PROFILE_PAYLOAD['user_id']
JOB = PageMessage(page_id=1, page_name=HANDLE, page_url=f'https://www.tiktok.com/@{HANDLE}', max_posts=50)


def aweme(aweme_id: str, *, create_time: int = 1_700_000_000, handle: str = HANDLE) -> dict:
    """One RAW upstream aweme, the shape the app's feed carries."""
    return {'aweme_id': aweme_id, 'desc': f'post {aweme_id}', 'create_time': create_time,
            'author': {'uid': USER_ID, 'unique_id': handle, 'nickname': 'Sirab'},
            'statistics': {'play_count': 10, 'digg_count': 2},
            'added_sound_music_info': {'id': '1', 'title': 'original sound'},
            'video': {'duration': 26411}, 'region': 'AZ'}


def feed(awemes, *, has_more: bool = True, max_cursor: int | None = 1_756_628_308_000) -> DeviceFeed:
    return DeviceFeed(user_id=USER_ID, aweme_list=tuple(awemes), has_more=has_more,
                      max_cursor=max_cursor, captured_at=1_000.0)


class FakeDriver:
    """The device boundary. Answers with a feed or raises — never a process."""

    def __init__(self, answer) -> None:
        self._answer = answer
        self.calls: list[str] = []

    def fetch_posts(self, user_id: str):
        self.calls.append(user_id)
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer


def run_consumer(deliveries, api: FakeApi, *, page_source=None, config: ConsumerConfig | None = None):
    """Drive one `BrokerConsumer.run()` over scripted deliveries.

    A local twin of `test_broker_consumer.run_consumer` with the `page_source`
    seam threaded through. The FAKES it drives are that module's — imported,
    not copied — so the pika and HTTP boundaries stay one contract; only the
    construction line differs, and adding a parameter to the shared helper
    would have changed a call signature 30 existing tests depend on."""
    channel = FakeChannel(deliveries)
    connection = FakeConnection(channel)
    consumer = BrokerConsumer(SETTINGS, api, config=BACKOFFS if config is None else config,
                              connect=lambda _settings: connection, page_source=page_source)
    consumer.run()
    return consumer, channel, connection


def source(answer, *, profile=None) -> tuple[DevicePageSource, FakeApi, FakeDriver]:
    api = FakeApi(profile=PROFILE_PAYLOAD if profile is None else profile)
    driver = FakeDriver(answer)
    return DevicePageSource(api, driver), api, driver


class TestTheDefaultSourcePreservesTodaysBehaviour:
    """A worker process is consuming the live broker right now. `--source`
    defaulting to anything but `search` would change what it does."""

    def test_the_default_is_search(self):
        assert worker_module.build_parser().parse_args([]).source == worker_module.SOURCE_SEARCH
        assert worker_module.SOURCE_SEARCH == 'search'

    def test_the_default_builds_no_page_source_at_all(self):
        # Not merely "search is available": the default must construct NO
        # driver and open NO spool, so the search worker cannot be affected by
        # anything in the device tree.
        args = worker_module.build_parser().parse_args([])
        assert worker_module.build_page_source(args, FakeApi()) is None

    def test_the_device_source_is_built_only_when_it_is_asked_for(self, tmp_path):
        args = worker_module.build_parser().parse_args(['--source', 'device', '--spool-dir', str(tmp_path)])
        built = worker_module.build_page_source(args, FakeApi())
        assert isinstance(built, DevicePageSource)

    def test_a_consumer_built_without_a_page_source_serves_page_jobs_over_http(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(2))
        _consumer, channel, _connection = run_consumer([(PAGE_QUEUE, page_body())], api)
        assert api.names == ['profile', 'user_posts'], 'the unchanged path is /profile + /user/posts'
        assert len(channel.published) == 2
        assert channel.acked == [1]

    def test_the_consumers_page_source_parameter_defaults_to_none(self):
        assert BrokerConsumer(SETTINGS, FakeApi())._page_source is None

    def test_an_invalid_device_knob_stops_the_worker_before_it_consumes(self, tmp_path):
        args = worker_module.build_parser().parse_args(['--source', 'device', '--spool-dir', str(tmp_path)])
        args.device_timeout = 0.0
        with pytest.raises(ValueError):
            worker_module.build_page_source(args, FakeApi())

    def test_the_timeout_flag_refuses_a_non_positive_value(self):
        with pytest.raises(SystemExit):
            worker_module.build_parser().parse_args(['--device-timeout', '0'])


class TestPageOnlyRouting:
    """`device` applies to PAGE jobs only. Keyword jobs stay on `POST /search`
    deliberately: driving in-app search needs UI text entry and is fragile,
    while opening a profile by intent is not."""

    def test_a_keyword_job_never_reaches_the_page_source(self):
        def _explode(job):
            raise AssertionError('a keyword job was routed to the device source')

        api = FakeApi(search=search_payload(3))
        channel = run_consumer([(KEYWORD_QUEUE, keyword_body())], api, page_source=_explode)[1]
        assert api.names == ['search']
        assert len(channel.published) == 3
        assert channel.acked == [1]

    def test_a_page_job_reaches_the_page_source_and_not_user_posts(self):
        seen: list = []

        def _record(job):
            seen.append(job)
            return []

        api = FakeApi()
        run_consumer([(PAGE_QUEUE, page_body())], api, page_source=_record)
        assert len(seen) == 1 and isinstance(seen[0], PageMessage)
        assert 'user_posts' not in api.names

    def test_both_queues_at_once_split_by_kind(self):
        pages: list = []
        api = FakeApi(search=search_payload(1))
        deliveries = [(KEYWORD_QUEUE, keyword_body()), (PAGE_QUEUE, page_body())]
        run_consumer(deliveries, api, page_source=lambda job: pages.append(job) or [])
        assert api.names == ['search'], 'only the keyword job went to the API'
        assert len(pages) == 1


class TestTheResolve:
    """A deep link addresses an account by numeric uid and a page job carries a
    handle. `POST /profile` already resolves that and costs one cap unit, so it
    is used rather than a second resolver being written."""

    def test_the_profile_endpoint_is_the_resolver_and_is_called_once(self):
        page_source, api, driver = source(feed([aweme('1')]))
        page_source(JOB)
        assert api.calls == [('profile', {'username': HANDLE})]
        assert driver.calls == [USER_ID], 'the driver is handed the resolved uid'

    def test_user_posts_is_never_called_on_this_path(self):
        page_source, api, _driver = source(feed([aweme('1')]))
        page_source(JOB)
        assert 'user_posts' not in api.names

    def test_a_profile_with_no_user_id_is_transient_not_a_dropped_job(self):
        # Our OWN API answering off its declared contract — classified the way
        # `api_client.results` classifies a response with no `results` list.
        page_source, _api, _driver = source(feed([]), profile={'username': HANDLE})
        with pytest.raises(ApiCallError) as caught:
            page_source(JOB)
        assert caught.value.failure is Failure.TRANSIENT

    def test_a_page_url_that_is_not_a_tiktok_profile_is_still_a_malformed_message(self):
        # The existing acked-and-dropped path, unchanged.
        job = PageMessage(page_id=1, page_name=HANDLE, page_url='https://example.com/@x', max_posts=5)
        page_source, _api, _driver = source(feed([]))
        with pytest.raises(MalformedMessage):
            page_source(job)


class TestFailureClassification:
    """Every device failure is TRANSIENT, so the EXISTING ack policy requeues
    the job. There is no second policy."""

    @pytest.mark.parametrize('failure', [HarvestTimeout('no response'), UnreadableResponse('0 bytes'),
                                         IntentFailed('container down'), UnusableUserId('nope')])
    def test_a_device_failure_is_transient(self, failure):
        page_source, _api, _driver = source(failure)
        with pytest.raises(ApiCallError) as caught:
            page_source(JOB)
        # The CLASSIFICATION, not merely the exception class: a PERMANENT
        # `ApiCallError` would ack the job and lose it.
        assert caught.value.failure is Failure.TRANSIENT

    def test_a_timeout_requeues_the_job_with_the_short_backoff(self):
        api = FakeApi(profile=PROFILE_PAYLOAD)
        page_source = DevicePageSource(api, FakeDriver(HarvestTimeout('no response')))
        _consumer, channel, connection = run_consumer([(PAGE_QUEUE, page_body())], api,
                                                      page_source=page_source, config=BACKOFFS)
        assert channel.nacked == [(1, True)]
        assert channel.acked == []
        assert connection.sleeps == [SHORT] and LONG not in connection.sleeps
        assert channel.published == [], 'nothing may be published for a visit that failed'

    def test_a_timeout_is_never_acked_as_a_job_that_found_nothing(self):
        api = FakeApi(profile=PROFILE_PAYLOAD)
        page_source = DevicePageSource(api, FakeDriver(HarvestTimeout('no response')))
        channel = run_consumer([(PAGE_QUEUE, page_body())], api, page_source=page_source)[1]
        assert channel.acked == []

    def test_an_empty_feed_is_a_success_that_publishes_nothing_and_acks(self):
        # The OTHER side of the same coin: a real response with an empty
        # `aweme_list` is a legitimate result. Zero posts is zero messages and
        # the job is done.
        api = FakeApi(profile=PROFILE_PAYLOAD)
        page_source = DevicePageSource(api, FakeDriver(feed([], has_more=False)))
        channel = run_consumer([(PAGE_QUEUE, page_body())], api, page_source=page_source)[1]
        assert channel.published == []
        assert channel.acked == [1]
        assert channel.nacked == []


class TestTheRecords:
    def test_every_aweme_becomes_one_envelope(self):
        page_source, _api, _driver = source(feed([aweme(str(i)) for i in range(1, 13)]))
        assert len(page_source(JOB)) == 12

    def test_records_go_through_flatten_video(self):
        page_source, _api, _driver = source(feed([aweme('7680105451131882770')]))
        record = page_source(JOB)[0].record
        assert record['id'] == '7680105451131882770'
        assert record['author_unique_id'] == HANDLE
        assert record['view_count'] == 10 and record['like_count'] == 2
        assert record['region_code'] == 'AZ'
        assert record['duration'] == 26411

    def test_a_duplicate_aweme_becomes_one_message_and_not_two(self):
        page_source, _api, _driver = source(feed([aweme('5'), aweme('5'), aweme('6')]))
        assert [e.record['id'] for e in page_source(JOB)] == ['5', '6']

    def test_an_aweme_with_no_id_is_dropped_rather_than_published_empty(self):
        page_source, _api, _driver = source(feed([{'desc': 'no id here'}, aweme('9')]))
        assert [e.record['id'] for e in page_source(JOB)] == ['9']

    def test_the_feed_is_ordered_newest_first(self):
        awemes = [aweme('old', create_time=1_600_000_000), aweme('new', create_time=1_800_000_000),
                  aweme('mid', create_time=1_700_000_000)]
        page_source, _api, _driver = source(feed(awemes))
        assert [e.record['id'] for e in page_source(JOB)] == ['new', 'mid', 'old']

    def test_the_ordering_is_the_one_the_api_publishes_today(self):
        # The MECHANISM that keeps this module's copy in step with
        # `api/app.py._newest_first` (which cannot be imported here: it drags
        # in the FastAPI app). A change to either that is not made to the
        # other fails right here.
        records = [flatten_video(aweme(str(i), create_time=1_600_000_000 + i * 1000), 'x')
                   for i in range(8)]
        records.append(flatten_video({'aweme_id': 'undated', 'author': {'unique_id': HANDLE},
                                      'statistics': {}}, 'x'))
        shuffled = records[3:] + records[:3]
        assert _newest_first(shuffled) == app_module._newest_first(shuffled)

    def test_an_undated_aweme_lands_last_and_never_claims_to_be_the_newest(self):
        awemes = [{'aweme_id': 'undated', 'author': {'unique_id': HANDLE}, 'statistics': {}},
                  aweme('dated', create_time=1_600_000_000)]
        page_source, _api, _driver = source(feed(awemes))
        assert [e.record['id'] for e in page_source(JOB)] == ['dated', 'undated']

    def test_the_trim_drops_the_least_recent_and_not_the_first_seen(self):
        # The app leads its grid with PINNED posts, so trimming the app's own
        # order to `max_posts` could drop this week's posts for a pin from two
        # years ago. The sort comes first for exactly that reason.
        job = PageMessage(page_id=1, page_name=HANDLE, page_url=None, max_posts=2)
        awemes = [aweme('pinned-old', create_time=1_500_000_000),
                  aweme('newest', create_time=1_800_000_000),
                  aweme('second', create_time=1_700_000_000)]
        page_source, _api, _driver = source(feed(awemes))
        assert [e.record['id'] for e in page_source(job)] == ['newest', 'second']

    def test_the_limit_is_the_jobs_own_max_posts(self):
        job = PageMessage(page_id=1, page_name=HANDLE, page_url=None, max_posts=3)
        page_source, _api, _driver = source(feed([aweme(str(i), create_time=1_600_000_000 + i) for i in range(10)]))
        assert len(page_source(job)) == 3

    def test_no_author_filter_is_applied_because_the_feed_is_addressed_by_uid(self):
        # `/aweme/v1/aweme/post/` is fetched BY user_id, so every aweme in the
        # response is that account's by construction. The records keep whatever
        # handle upstream gave them rather than being silently dropped.
        records = device_records([aweme('1', handle='someone-else')], USER_ID, limit=10)
        assert [r['author_unique_id'] for r in records] == ['someone-else']


class TestTheSourceTerm:
    """There is no keyword on this path. `device:<user_id>` is the value, and
    it follows `client.POSTS_SOURCE_PREFIX`'s established convention for this
    same endpoint reached by a signed request (`posts:<user_id>`)."""

    def test_it_names_the_source_and_the_uid(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        envelope = page_source(JOB)[0]
        assert envelope.metadata.source_term == f'{DEVICE_SOURCE_PREFIX}{USER_ID}'

    def test_it_cannot_be_read_as_a_keyword(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        term = page_source(JOB)[0].metadata.source_term
        assert term.startswith('device:'), 'the prefix names the SOURCE'
        assert term != HANDLE and term != JOB.page_name
        assert not term.isalpha(), 'it is not word-shaped and cannot be mistaken for a search term'

    def test_no_keyword_field_is_invented_for_a_page_job(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        metadata = page_source(JOB)[0].metadata
        assert metadata.keyword_id is None and metadata.keyword_name is None

    def test_it_is_in_metadata_and_not_at_the_top_level(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        wire = body(page_source(JOB)[0])
        assert 'source_term' not in wire
        assert wire['metadata']['source_term'] == f'{DEVICE_SOURCE_PREFIX}{USER_ID}'


class TestTheEnvelopeShapeIsUnchanged:
    """A consumer must not be able to tell which source produced a message."""

    def test_the_wire_keys_match_the_search_paths_for_the_same_post(self):
        raw = aweme('7680105451131882770')
        page_source, _api, _driver = source(feed([raw]))
        device_wire = body(page_source(JOB)[0])

        from tiktoksearch.broker.envelope import page_envelopes
        searched = flatten_video(dict(raw), f'search:{HANDLE}')
        search_wire = body(page_envelopes(JOB, [searched], PROFILE_PAYLOAD)[0])

        assert set(device_wire) == set(search_wire)
        assert set(device_wire['metadata']) == set(search_wire['metadata'])
        assert set(device_wire['metadata']['profile']) == set(search_wire['metadata']['profile'])

    def test_search_type_is_page_because_the_queue_is_what_it_names(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        assert body(page_source(JOB)[0])['search_type'] == 'page'

    def test_post_url_is_built_from_the_identity_field(self):
        page_source, _api, _driver = source(feed([aweme('7680105451131882770')]))
        assert body(page_source(JOB)[0])['post_url'] == f'https://www.tiktok.com/@{HANDLE}/video/7680105451131882770'

    def test_our_internal_diagnostics_are_still_stripped_from_the_profile(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        profile = body(page_source(JOB)[0])['metadata']['profile']
        assert 'device' not in profile and 'elapsed_s' not in profile
        assert profile['profile_url'] == f'https://www.tiktok.com/@{HANDLE}'

    def test_the_job_ids_are_echoed(self):
        page_source, _api, _driver = source(feed([aweme('1')]))
        metadata = body(page_source(JOB)[0])['metadata']
        assert metadata['page_id'] == 1 and metadata['page_name'] == HANDLE


class TestNoSecretIsLogged:
    def test_the_device_path_logs_no_credential(self, caplog):
        caplog.set_level('DEBUG', logger='tiktoksearch.broker.device_source')
        page_source, _api, _driver = source(feed([aweme('1')]))
        page_source(JOB)
        for forbidden in ('sessionid', 'x_tt_token', 'RABBITMQ_PASSWORD', 'identities.json'):
            assert forbidden not in caplog.text

    def test_the_driver_config_never_carries_a_credential(self, tmp_path):
        rendered = repr(DeviceConfig(spool_dir=str(tmp_path)))
        assert 'cookie' not in rendered and 'token' not in rendered
