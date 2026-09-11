"""Unit tests for the broker's I/O edges: `consumer.py`, `api_client.py`, `env.py`.

**NOTHING HERE TOUCHES A BROKER OR A NETWORK.** There is a live RabbitMQ at the
address the plan measured and a worker consuming from it right now; a test that
connected would publish into a production queue. Two seams make that
structurally impossible and both are used by every test below:

* `BrokerConsumer(connect=...)` — the pika boundary exists as an injected
  callable for exactly this reason. Every test passes `FakeConnection`.
* `ApiClient(session=...)` — the HTTP boundary. Every test passes `FakeSession`.

On top of those, `_forbid_a_real_broker_connection` (autouse, this module)
replaces `pika.BlockingConnection` with a tripwire, so even a future test that
forgot to inject `connect` fails loudly instead of dialling the broker. It is
the broker analogue of conftest's session-wide `_forbid_real_network`, which
also still stands over every test here.

The repo-root `.env` holds the LIVE broker password. No test reads it: every
`broker_settings` case passes an explicit `environ` mapping and an `env_path`
under `tmp_path`, and no assertion anywhere names a real value. Every
credential in this file is an obvious fake.

What is pinned, in the order the plan's ack table lists it:

| outcome | action |
|---|---|
| every post message published | ack |
| permanent (404 / 422 / malformed body) | ack |
| transient (502 / 503 / unreachable / publish failure) | nack(requeue=True) + SHORT backoff |
| 429 daily cap exhausted | nack(requeue=True) + LONG backoff |
| 429 TikTok rate-limited | nack(requeue=True) + SHORT backoff |

Both 429s are the same status, so the discrimination is a prefix match on the
`detail` literal `api/app.py` emits — and the FALL-THROUGH direction is pinned
too: an unrecognised 429 must take the cap/long path, because over-waiting is
cheap and hammering an exhausted daily cap is not.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pika
import pytest
import requests
from pika.exceptions import AMQPConnectionError, NackError, UnroutableError

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch.broker import api_client as api_module  # noqa: E402
from tiktoksearch.broker import consumer as consumer_module  # noqa: E402
from tiktoksearch.broker import env as env_module  # noqa: E402
from tiktoksearch.broker.api_client import (  # noqa: E402
    MAX_LOGGED_DETAIL_CHARS,
    RATE_LIMITED_DETAIL_PREFIX,
    RESULTS_KEY,
    ApiCallError,
    ApiClient,
    Failure,
    results,
)
from tiktoksearch.broker.consumer import (  # noqa: E402
    DEFAULT_PREFETCH,
    DELIVERY_MODE_PERSISTENT,
    KEYWORD_QUEUE,
    LONG_BACKOFF_S,
    PAGE_QUEUE,
    QUEUE_SELECTIONS,
    RESULT_CONTENT_TYPE,
    RESULT_ROUTING_KEY,
    SHORT_BACKOFF_S,
    BrokerConsumer,
    ConsumerConfig,
    _parse,
)
from tiktoksearch.broker.env import (  # noqa: E402
    DEFAULT_ENV_PATH,
    DEFAULT_EXCHANGE,
    DEFAULT_PORT,
    DEFAULT_VHOST,
    BrokerSettings,
    broker_settings,
    parse_env,
    read_env_file,
)
from tiktoksearch.broker.errors import BrokerConfigError, MalformedMessage  # noqa: E402
from tiktoksearch.filters import PublishTime, SortType  # noqa: E402

# --------------------------------------------------------------------------
# Obvious fakes. Nothing here is shaped like a real credential, and the host
# is in the reserved `.invalid` TLD so it cannot resolve even by accident.
# --------------------------------------------------------------------------
FAKE_HOST = 'fake-broker.invalid'
FAKE_USER = 'fake-worker-user'
FAKE_PASSWORD = 'not-a-real-password'
# Deliberately NOT the measured exchange name: a publish test asserting the
# real default cannot tell "read from settings" from "hard-coded literal".
SENTINEL_EXCHANGE = 'sentinel.exchange.for.tests'

SETTINGS = BrokerSettings(host=FAKE_HOST, port=5672, vhost='/', user=FAKE_USER, password=FAKE_PASSWORD, exchange=SENTINEL_EXCHANGE)

# The two live inbound bodies, verbatim from `implementation_plan.md`
# § Measured broker facts (see `test_broker_messages.py` for the same pair).
LIVE_KEYWORD_BODY = {
    'keyword_id': 5038, 'keyword_name': 'Şəki', 'is_auto_generated': False,
    'is_combined': False, 'max_results': 30, 'sort_type': None,
    'publish_time': None, 'timestamp': '2026-09-09T11:52:33.589249',
}
LIVE_PAGE_BODY = {
    'page_id': 1, 'page_name': 'sirabasc',
    'page_url': 'https://www.tiktok.com/@sirabasc',
    'max_posts': 50, 'timestamp': '2026-09-09T11:52:43.447566',
}

HANDLE = 'sirabasc'
PROFILE_PAYLOAD = {'username': HANDLE, 'user_id': '7195575867517944837', 'display_name': 'Sirab', 'device': 'FAKE-DEV-1', 'elapsed_s': 1.5}

# Distinct sentinel backoffs, so an assertion says WHICH knob was chosen
# rather than how many seconds elapsed. Nothing sleeps for real: the wait goes
# through `connection.sleep`, which is `FakeConnection`'s recorder.
SHORT = 0.5
LONG = 9.0
BACKOFFS = ConsumerConfig(short_backoff_s=SHORT, long_backoff_s=LONG)


def keyword_body(**over) -> bytes:
    return json.dumps({**LIVE_KEYWORD_BODY, **over}).encode('utf-8')


def page_body(**over) -> bytes:
    return json.dumps({**LIVE_PAGE_BODY, **over}).encode('utf-8')


def post(aweme_id: str = '7680105451131882770', *, handle: str = HANDLE) -> dict:
    """A minimal `flatten_video`-shaped record. Only the two fields `post_url`
    is built from need real values here; the full record shape is
    `test_broker_envelope.py`'s subject."""
    return {'id': aweme_id, 'author_unique_id': handle, 'author_username': handle, 'source_term': f'search:{handle}'}


def search_payload(count: int = 1) -> dict:
    return {'count': count, RESULTS_KEY: [post(str(7_680_105_451_131_880_000 + n)) for n in range(count)]}


# ============================================================ the pika seam
@pytest.fixture(autouse=True)
def _forbid_a_real_broker_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live broker is reachable from this machine and a worker is consuming
    from it. A test that connected would publish into a production queue.

    `BrokerConsumer`'s `connect` is injected precisely so that never has to
    happen; this tripwire covers the case where a future test forgets to
    inject it, exactly as conftest's `_forbid_real_network` covers a forgotten
    transport stub."""

    def _tripwire(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError('a test tried to open a REAL AMQP connection — inject BrokerConsumer(connect=...) instead')

    monkeypatch.setattr(pika, 'BlockingConnection', _tripwire)
    monkeypatch.setattr(consumer_module.pika, 'BlockingConnection', _tripwire)


class Method:
    """pika's delivery `method` frame, reduced to the one field read.

    `routing_key` carries a value the consumer must NOT use to pick a handler —
    which queue's parser runs is decided by what we subscribed to. It is set to
    a wrong-on-purpose value so a handler chosen from it fails."""

    def __init__(self, delivery_tag: int) -> None:
        self.delivery_tag = delivery_tag
        self.routing_key = 'never.read.this'


class FakeChannel:
    """A pika channel that DECLARES NOTHING and says so loudly.

    `queue_declare` / `exchange_declare` / `queue_purge` raise. The topology
    already exists on the live broker, and a declare from the worker would
    either fight the producer's settings or fail the channel on a mismatch —
    so "the worker declares nothing" is asserted by making the call impossible
    rather than by counting calls that were never made."""

    def __init__(self, deliveries=(), *, events=None, publish_error=None, publish_fails_at=None) -> None:
        self.deliveries = list(deliveries)
        self.events = [] if events is None else events
        self.publish_error = publish_error
        self.publish_fails_at = publish_fails_at
        self.consumers: dict = {}
        self.consume_order: list[str] = []
        self.published: list[dict] = []
        self.acked: list[int] = []
        self.nacked: list[tuple] = []
        self.qos: list[int] = []
        self.confirms = 0
        self.stopped = 0

    # --- the declarations that must never happen -------------------------
    def queue_declare(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError('the worker must not declare a queue')

    def exchange_declare(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError('the worker must not declare an exchange')

    def queue_purge(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError('the worker must never purge a queue')

    def queue_bind(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError('the worker must not bind a queue')

    # --- the calls it does make ------------------------------------------
    def confirm_delivery(self) -> None:
        self.confirms += 1

    def basic_qos(self, prefetch_count: int) -> None:
        self.qos.append(prefetch_count)

    def basic_consume(self, queue: str, on_message_callback) -> None:  # noqa: ANN001
        self.consumers[queue] = on_message_callback
        self.consume_order.append(queue)

    def basic_publish(self, exchange, routing_key, body, properties, mandatory=False):  # noqa: ANN001
        if self.publish_error is not None and (self.publish_fails_at is None or len(self.published) == self.publish_fails_at):
            raise self.publish_error
        self.published.append({'exchange': exchange, 'routing_key': routing_key, 'body': body, 'properties': properties, 'mandatory': mandatory})

    def basic_ack(self, delivery_tag: int) -> None:
        self.events.append(('ack', delivery_tag))
        self.acked.append(delivery_tag)

    def basic_nack(self, delivery_tag: int, requeue: bool) -> None:
        self.events.append(('nack', delivery_tag, requeue))
        self.nacked.append((delivery_tag, requeue))

    def stop_consuming(self) -> None:
        self.stopped += 1

    def start_consuming(self) -> None:
        for index, (queue, body) in enumerate(self.deliveries, start=1):
            if self.stopped:
                break
            self.consumers[queue](self, Method(index), None, body)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(entry['body'].decode('utf-8')) for entry in self.published]


class FakeConnection:
    """pika's `BlockingConnection`, reduced to what `run` and `_nack_after`
    touch. `sleep` RECORDS instead of sleeping, so the backoff a path chose is
    observable and the suite stays fast."""

    def __init__(self, channel: FakeChannel) -> None:
        self._channel = channel
        self.sleeps: list[float] = []
        self.is_open = True
        self.closed = 0

    def channel(self) -> FakeChannel:
        return self._channel

    def sleep(self, seconds: float) -> None:
        self._channel.events.append(('sleep', seconds))
        self.sleeps.append(seconds)

    def close(self) -> None:
        self.closed += 1
        self.is_open = False


class FakeApi:
    """The `ApiClient` seam. Each of the three calls is a payload dict, an
    exception INSTANCE to raise, or a list of either consumed in order."""

    def __init__(self, *, search=None, profile=None, posts=None) -> None:
        self._answers = {'search': search, 'profile': profile, 'posts': posts}
        self.calls: list[tuple] = []

    def _answer(self, name: str):
        answer = self._answers[name]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise AssertionError(f'the test did not script {name}()')
        return answer

    def search(self, *, query, limit, sort_type=None, publish_time=None):  # noqa: ANN001
        self.calls.append(('search', {'query': query, 'limit': limit, 'sort_type': sort_type, 'publish_time': publish_time}))
        return self._answer('search')

    def profile(self, *, username):  # noqa: ANN001
        self.calls.append(('profile', {'username': username}))
        return self._answer('profile')

    def user_posts(self, *, username, limit):  # noqa: ANN001
        self.calls.append(('user_posts', {'username': username, 'limit': limit}))
        return self._answer('posts')

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def run_consumer(deliveries, api: FakeApi, *, config: ConsumerConfig | None = None, settings: BrokerSettings = SETTINGS, **channel_over) -> tuple[BrokerConsumer, FakeChannel, FakeConnection]:
    """Drive one `BrokerConsumer.run()` over scripted deliveries."""
    channel = FakeChannel(deliveries, **channel_over)
    connection = FakeConnection(channel)
    consumer = BrokerConsumer(settings, api, config=BACKOFFS if config is None else config, connect=lambda _settings: connection)
    consumer.run()
    return consumer, channel, connection


def keyword_run(api: FakeApi, **kwargs) -> tuple[BrokerConsumer, FakeChannel, FakeConnection]:
    return run_consumer([(KEYWORD_QUEUE, keyword_body())], api, **kwargs)


def page_run(api: FakeApi, **kwargs) -> tuple[BrokerConsumer, FakeChannel, FakeConnection]:
    return run_consumer([(PAGE_QUEUE, page_body())], api, **kwargs)


# ================================================ 1: the worker declares nothing
class TestTheWorkerDeclaresNothing:
    """The topology already exists — exchange `sm.scraping.tiktok`, direct,
    durable, four queues whose routing key equals their name. A declare from
    here would fight the producer's settings or fail the channel."""

    def test_a_successful_run_fires_no_declaration(self):
        # `FakeChannel.queue_declare` / `exchange_declare` / `queue_purge` /
        # `queue_bind` all RAISE, so this run completing at all is the
        # assertion. A counting stub would pass for a worker that declared
        # something and swallowed the result.
        _, channel, _ = keyword_run(FakeApi(search=search_payload(2)))
        assert channel.acked == [1]
        assert len(channel.published) == 2

    def test_the_declaration_tripwires_really_do_fire(self):
        # Otherwise the test above proves nothing: a stub whose tripwires were
        # broken would let a declare through silently.
        channel = FakeChannel()
        for call in (channel.queue_declare, channel.exchange_declare, channel.queue_purge, channel.queue_bind):
            with pytest.raises(AssertionError):
                call('sm.scraping.tiktok.keyword')

    def test_publisher_confirms_are_turned_on(self):
        # Without them `basic_publish` is fire-and-forget and the plan's safety
        # property — "acked only after every outbound message is published" —
        # would be untrue.
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.confirms == 1

    def test_qos_is_set_to_the_configured_prefetch(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)), config=ConsumerConfig(prefetch=1))
        assert channel.qos == [1]
        assert DEFAULT_PREFETCH == 1, 'one message at a time; concurrency is out of scope'

    def test_it_subscribes_to_exactly_the_configured_queues(self):
        api = FakeApi(search=search_payload(1))
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, keyword_body())], api, config=ConsumerConfig(queues=(KEYWORD_QUEUE,)))
        assert channel.consume_order == [KEYWORD_QUEUE]

    def test_both_queues_are_subscribed_by_default(self):
        assert ConsumerConfig().queues == (KEYWORD_QUEUE, PAGE_QUEUE)

    def test_the_connection_is_closed_on_the_way_out(self):
        # An in-flight job's message is unacked at that point, so closing
        # requeues it rather than losing it.
        _, _, connection = keyword_run(FakeApi(search=search_payload(1)))
        assert connection.closed == 1

    def test_the_queue_names_are_the_measured_ones(self):
        assert KEYWORD_QUEUE == 'sm.scraping.tiktok.keyword'
        assert PAGE_QUEUE == 'sm.scraping.tiktok.page'

    def test_the_re_post_queue_is_not_selectable_or_publishable(self):
        # `sm.scraping.tiktok.re.post` is NOT ours: the worker never consumes
        # it and never publishes to it.
        foreign = 'sm.scraping.tiktok.re.post'
        assert foreign not in {queue for queues in QUEUE_SELECTIONS.values() for queue in queues}
        assert foreign not in (KEYWORD_QUEUE, PAGE_QUEUE, RESULT_ROUTING_KEY)
        assert foreign not in consumer_module._PARSERS


class TestTheHandlerIsChosenByTheSubscription:
    """`queue` is bound into the callback rather than read off
    `method.routing_key`, so which parser runs is decided by what we
    subscribed to and never by a value carried on the message."""

    def test_a_page_body_delivered_on_the_keyword_queue_is_not_parsed_as_a_page_job(self):
        # If the handler were picked from the message, this body would parse
        # cleanly as a `PageMessage` and two API calls would follow. Parsed as
        # a `KeywordMessage` it fails validation and is acked — and NOTHING is
        # called.
        api = FakeApi()
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, page_body())], api)
        assert api.names == []
        assert channel.acked == [1]
        assert channel.published == []

    def test_each_queue_gets_its_own_callback(self):
        api = FakeApi(search=search_payload(1), profile=PROFILE_PAYLOAD, posts=search_payload(1))
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, keyword_body()), (PAGE_QUEUE, page_body())], api)
        assert api.names == ['search', 'profile', 'user_posts']
        assert channel.acked == [1, 2]


# ======================================================== 2: the ack policy
class TestSuccessAcks:
    def test_a_served_keyword_job_is_acked_once(self):
        _, channel, connection = keyword_run(FakeApi(search=search_payload(3)))
        assert channel.acked == [1]
        assert channel.nacked == []
        assert connection.sleeps == []

    def test_a_served_page_job_is_acked_once(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(2))
        _, channel, _ = page_run(api)
        assert channel.acked == [1]
        assert channel.nacked == []

    def test_the_ack_carries_the_delivery_tag_of_that_message(self):
        api = FakeApi(search=[search_payload(1), search_payload(1)])
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, keyword_body()), (KEYWORD_QUEUE, keyword_body())], api)
        assert channel.acked == [1, 2]

    def test_zero_posts_is_a_success_not_an_error(self):
        # "One message per post" means zero posts is zero messages: nothing
        # published, the message ACKED, one INFO line.
        _, channel, connection = keyword_run(FakeApi(search={'count': 0, RESULTS_KEY: []}))
        assert channel.published == []
        assert channel.acked == [1]
        assert channel.nacked == []
        assert connection.sleeps == []

    def test_zero_posts_on_a_page_job_is_a_success_too(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts={RESULTS_KEY: []})
        _, channel, _ = page_run(api)
        assert channel.published == []
        assert channel.acked == [1]


class TestPermanentErrorsAck:
    """A request the API rejects will be rejected identically on every
    redelivery, so requeueing would loop forever."""

    @pytest.mark.parametrize('status', [404, 422, 400, 405, 409])
    def test_a_permanent_failure_is_acked_and_dropped(self, status):
        api = FakeApi(search=ApiCallError(Failure.PERMANENT, f'/search {status}', status=status))
        _, channel, connection = keyword_run(api)
        assert channel.acked == [1]
        assert channel.nacked == []
        assert connection.sleeps == [], 'no point backing off before dropping'

    def test_nothing_is_published_for_a_permanent_failure(self):
        api = FakeApi(search=ApiCallError(Failure.PERMANENT, '/search 404', status=404))
        _, channel, _ = keyword_run(api)
        assert channel.published == []

    def test_a_permanent_failure_on_the_profile_call_stops_the_page_job(self):
        # The profile is on every message of a page job, so there is nothing to
        # publish without it — and `/user/posts` must not be called.
        api = FakeApi(profile=ApiCallError(Failure.PERMANENT, '/profile 404', status=404))
        _, channel, _ = page_run(api)
        assert api.names == ['profile']
        assert channel.acked == [1]
        assert channel.published == []


class TestTransientErrorsNackWithRequeue:
    @pytest.mark.parametrize('message', ['/search 502', '/search 503', '/search unreachable (ConnectionError)'])
    def test_a_transient_failure_is_requeued(self, message):
        api = FakeApi(search=ApiCallError(Failure.TRANSIENT, message))
        _, channel, _ = keyword_run(api)
        assert channel.nacked == [(1, True)], 'requeue=True, or the job is lost'
        assert channel.acked == []

    def test_a_transient_failure_takes_the_short_backoff(self):
        api = FakeApi(search=ApiCallError(Failure.TRANSIENT, '/search 503', status=503))
        _, _, connection = keyword_run(api)
        assert connection.sleeps == [SHORT]

    def test_the_backoff_is_spent_before_the_nack(self):
        # While the message is unacked the broker cannot redeliver it, so the
        # wait is actually observed. Nack first and, at prefetch 1, the same
        # message comes straight back and the wait protects nothing.
        api = FakeApi(search=ApiCallError(Failure.TRANSIENT, '/search 503', status=503))
        _, channel, _ = keyword_run(api)
        assert channel.events == [('sleep', SHORT), ('nack', 1, True)]

    def test_nothing_is_published_for_a_transient_failure(self):
        api = FakeApi(search=ApiCallError(Failure.TRANSIENT, '/search 503'))
        _, channel, _ = keyword_run(api)
        assert channel.published == []


class TestTheTwo429sTakeDifferentBackoffs:
    """Both are HTTP 429, and only the `detail` prose separates a rate-limit
    from an exhausted daily cap. The cap resets on a UTC day boundary, so
    retrying it in seconds is pure waste."""

    def test_an_exhausted_cap_takes_the_long_backoff(self):
        api = FakeApi(search=ApiCallError(Failure.CAP_EXHAUSTED, '/search 429 daily cap', status=429))
        _, channel, connection = keyword_run(api)
        assert connection.sleeps == [LONG]
        assert channel.nacked == [(1, True)]

    def test_a_rate_limit_takes_the_short_backoff(self):
        api = FakeApi(search=ApiCallError(Failure.TRANSIENT, '/search 429 rate-limited', status=429))
        _, channel, connection = keyword_run(api)
        assert connection.sleeps == [SHORT]
        assert channel.nacked == [(1, True)]

    def test_the_two_backoffs_are_not_the_same_knob(self):
        # Without this the two tests above would both pass against a consumer
        # that used one backoff for everything.
        assert SHORT != LONG
        assert BACKOFFS.short_backoff_s != BACKOFFS.long_backoff_s

    def test_neither_429_is_ever_acked(self):
        for failure in (Failure.CAP_EXHAUSTED, Failure.TRANSIENT):
            api = FakeApi(search=ApiCallError(failure, '/search 429', status=429))
            _, channel, _ = keyword_run(api)
            assert channel.acked == [], failure

    def test_the_shipped_backoffs_are_ordered_and_under_the_consumer_timeout(self):
        assert SHORT_BACKOFF_S == 5.0
        assert LONG_BACKOFF_S == 900.0
        assert LONG_BACKOFF_S > SHORT_BACKOFF_S
        # The backoff is spent with the message UNACKED, so exceeding
        # RabbitMQ's default 30-minute `consumer_timeout` would have the broker
        # close the channel instead of the worker retrying.
        assert LONG_BACKOFF_S < 1800

    def test_the_default_config_uses_the_shipped_backoffs(self):
        api = FakeApi(search=ApiCallError(Failure.CAP_EXHAUSTED, '/search 429', status=429))
        _, _, connection = keyword_run(api, config=ConsumerConfig())
        assert connection.sleeps == [LONG_BACKOFF_S]


class TestAPublishFailureRequeuesTheWholeJob:
    """Inbound messages are acked only after EVERY outbound message is
    published, so a crash or a rejection mid-job requeues the whole job rather
    than half-publishing it."""

    @pytest.mark.parametrize('error', [UnroutableError([]), NackError([]), AMQPConnectionError()])
    def test_a_broker_rejection_nacks_with_requeue(self, error):
        api = FakeApi(search=search_payload(2))
        _, channel, connection = keyword_run(api, publish_error=error)
        assert channel.acked == []
        assert channel.nacked == [(1, True)]
        assert connection.sleeps == [SHORT]

    def test_a_failure_on_the_second_of_three_posts_still_requeues_the_job(self):
        # Half a job's results are published and the job comes back whole. That
        # is the accepted trade: a duplicate is recoverable, a silently lost
        # result is not.
        api = FakeApi(search=search_payload(3))
        _, channel, _ = keyword_run(api, publish_error=UnroutableError([]), publish_fails_at=1)
        assert len(channel.published) == 1
        assert channel.acked == []
        assert channel.nacked == [(1, True)]


class TestAnUnparseableBodyIsAckedNeverRequeued:
    """A body that will not parse will not parse on redelivery either."""

    @pytest.mark.parametrize('body', [b'', b'not json at all', b'{', b'[]', b'null', b'"a string"', b'{"keyword_id": 1'])
    def test_a_body_that_is_not_a_json_object_is_acked(self, body):
        api = FakeApi()
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, body)], api)
        assert channel.acked == [1]
        assert channel.nacked == []
        assert api.names == [], 'nothing may be spent on an unparseable body'

    def test_a_body_that_is_not_utf8_is_acked(self):
        api = FakeApi()
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, b'\xff\xfe{"keyword_id": 1}')], api)
        assert channel.acked == [1]
        assert channel.nacked == []

    @pytest.mark.parametrize('field', ['keyword_id', 'keyword_name', 'max_results'])
    def test_a_body_missing_a_required_field_is_acked(self, field):
        body = {key: value for key, value in LIVE_KEYWORD_BODY.items() if key != field}
        api = FakeApi()
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, json.dumps(body).encode())], api)
        assert channel.acked == [1]
        assert api.names == []

    def test_a_page_url_that_is_not_a_tiktok_url_is_acked_without_a_single_call(self):
        # `handle_from_page` refuses rather than falling back to `page_name`:
        # the job disagrees with itself about which account it is for.
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        _, channel, _ = run_consumer([(PAGE_QUEUE, page_body(page_url='https://www.instagram.com/@sirabasc'))], api)
        assert api.names == [], 'no signed request may be spent on a self-contradictory job'
        assert channel.acked == [1]
        assert channel.nacked == []

    def test_an_unparseable_body_publishes_nothing(self):
        _, channel, _ = run_consumer([(KEYWORD_QUEUE, b'{}')], FakeApi())
        assert channel.published == []

    def test_the_warning_names_the_field_and_not_the_producers_value(self, caplog):
        # `ValidationError.errors()` carries the rejected `input` — the
        # producer's own strings — so only the FIELD LOCATIONS are logged.
        caplog.set_level('WARNING', logger='tiktoksearch.broker.consumer')
        body = {**LIVE_KEYWORD_BODY, 'max_results': 'PRODUCER-SUPPLIED-GARBAGE'}
        run_consumer([(KEYWORD_QUEUE, json.dumps(body).encode())], FakeApi())
        assert 'max_results' in caplog.text
        assert 'PRODUCER-SUPPLIED-GARBAGE' not in caplog.text

    def test_a_body_that_is_not_json_at_all_still_names_something(self, caplog):
        # `loc` is EMPTY for a non-JSON body, so the error `type` stands in —
        # otherwise the line read "1 validation error(s) on: " and named
        # nothing at all.
        caplog.set_level('WARNING', logger='tiktoksearch.broker.consumer')
        run_consumer([(KEYWORD_QUEUE, b'not json at all')], FakeApi())
        assert 'json_invalid' in caplog.text

    def test_the_keyword_name_is_never_logged(self, caplog):
        caplog.set_level('DEBUG', logger='tiktoksearch.broker.consumer')
        keyword_run(FakeApi(search=search_payload(1)))
        assert 'Şəki' not in caplog.text
        assert 'keyword_id=5038' in caplog.text, 'the id IS the correlation handle'


class TestAMissingResultsListIsTransientAndNotZeroPosts:
    """A `results` key that is MISSING or not a list is not zero posts — it is
    a response that does not match the contract.

    Reporting an empty success for a reply we could not understand is exactly
    the failure `.claude/rules/anti-block.md` exists to forbid, and it is the
    difference between "this job found nothing" and "we published nothing and
    threw the job away"."""

    @pytest.mark.parametrize('payload', [{}, {'count': 0}, {RESULTS_KEY: None}, {RESULTS_KEY: {}}, {RESULTS_KEY: 'none'}, {RESULTS_KEY: 0}])
    def test_a_keyword_reply_without_a_results_list_is_requeued(self, payload):
        _, channel, connection = keyword_run(FakeApi(search=payload))
        assert channel.nacked == [(1, True)]
        assert channel.acked == [], 'acking here throws a good job away'
        assert connection.sleeps == [SHORT]

    @pytest.mark.parametrize('payload', [{}, {RESULTS_KEY: None}])
    def test_a_page_reply_without_a_results_list_is_requeued(self, payload):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=payload)
        _, channel, _ = page_run(api)
        assert channel.nacked == [(1, True)]
        assert channel.acked == []

    def test_an_empty_results_list_is_the_success_case_instead(self):
        # The discrimination, on one line: `[]` acks, absent nacks.
        _, empty, _ = keyword_run(FakeApi(search={RESULTS_KEY: []}))
        _, absent, _ = keyword_run(FakeApi(search={}))
        assert (empty.acked, empty.nacked) == ([1], [])
        assert (absent.acked, absent.nacked) == ([], [(1, True)])


# ======================================================== 3: the publish call
class TestThePublishCall:
    def test_it_uses_the_exchange_from_the_settings(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.published[0]['exchange'] == SENTINEL_EXCHANGE
        assert SENTINEL_EXCHANGE != DEFAULT_EXCHANGE, 'a sentinel, so a hard-coded default would fail here'

    def test_the_shipped_default_exchange_is_the_measured_one(self):
        assert DEFAULT_EXCHANGE == 'sm.scraping.tiktok'

    def test_it_uses_the_single_result_routing_key(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.published[0]['routing_key'] == 'sm.scraping.tiktok.keyword.result'
        assert RESULT_ROUTING_KEY == 'sm.scraping.tiktok.keyword.result'

    def test_a_page_result_goes_to_the_same_keyword_result_key(self):
        # ALL results go there — keyword and page alike, confirmed by the
        # requester — which is why the name says `keyword.result` for both.
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        _, channel, _ = page_run(api)
        assert channel.published[0]['routing_key'] == RESULT_ROUTING_KEY

    def test_the_message_is_persistent(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.published[0]['properties'].delivery_mode == 2
        assert DELIVERY_MODE_PERSISTENT == 2, 'matching the delivery_mode the inbound messages arrive with'

    def test_the_content_type_is_json(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.published[0]['properties'].content_type == 'application/json'
        assert RESULT_CONTENT_TYPE == 'application/json'

    def test_it_is_mandatory_so_an_unbound_routing_key_raises(self):
        # With confirms on, `mandatory=True` turns a routing key nothing is
        # bound to into an exception instead of a message the broker silently
        # discards. A loud requeue beats losing every result.
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        assert channel.published[0]['mandatory'] is True

    def test_two_hundred_posts_become_two_hundred_messages_with_distinct_urls(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(200))
        _, channel, _ = page_run(api)
        assert len(channel.published) == 200
        urls = [message['post_url'] for message in channel.bodies]
        assert len(set(urls)) == 200
        assert all(url and url.startswith('https://www.tiktok.com/@') for url in urls)
        assert channel.acked == [1]

    def test_the_body_is_utf8_json_carrying_the_contract_keys(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        _, channel, _ = page_run(api)
        published = channel.bodies[0]
        assert published['search_type'] == 'page'
        assert published['metadata']['page_id'] == 1
        assert published['metadata']['profile']['username'] == HANDLE
        assert 'device' not in published['metadata']['profile']

    def test_a_keyword_result_carries_the_keyword_ids_and_no_profile(self):
        _, channel, _ = keyword_run(FakeApi(search=search_payload(1)))
        metadata = channel.bodies[0]['metadata']
        assert (metadata['keyword_id'], metadata['keyword_name']) == (5038, 'Şəki')
        assert metadata['profile'] is None


# ================================================= 4: what a job actually asks
class TestWhatTheJobAsksTheApiFor:
    def test_a_keyword_job_searches_its_name_at_its_limit(self):
        api = FakeApi(search=search_payload(1))
        keyword_run(api)
        assert api.calls[0][1]['query'] == 'Şəki'
        assert api.calls[0][1]['limit'] == 30

    def test_the_jobs_filters_are_forwarded_when_present(self):
        api = FakeApi(search=search_payload(1))
        run_consumer([(KEYWORD_QUEUE, keyword_body(sort_type='1', publish_time='30'))], api)
        assert api.calls[0][1]['sort_type'] is SortType.MOST_LIKED
        assert api.calls[0][1]['publish_time'] is PublishTime.LAST_MONTH

    def test_null_filters_are_forwarded_as_none_and_never_as_a_default(self):
        api = FakeApi(search=search_payload(1))
        keyword_run(api)
        assert api.calls[0][1]['sort_type'] is None
        assert api.calls[0][1]['publish_time'] is None

    def test_a_page_job_fetches_the_profile_first_then_the_posts(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        page_run(api)
        assert api.names == ['profile', 'user_posts']

    def test_both_page_calls_use_the_handle_from_the_url_not_the_name(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        run_consumer([(PAGE_QUEUE, page_body(page_url='https://www.tiktok.com/@fromtheurl', page_name='fromthename'))], api)
        assert [call[1]['username'] for call in api.calls] == ['fromtheurl', 'fromtheurl']

    def test_the_posts_call_uses_max_posts_as_its_limit(self):
        api = FakeApi(profile=PROFILE_PAYLOAD, posts=search_payload(1))
        page_run(api)
        assert api.calls[1][1]['limit'] == 50


class TestAQueueWithNoParserIsOurBugAndNotAMessageToDrop:
    """A subscription this module has no handler for is a config error, so it
    escapes the ack policy and stops the worker with the job still unacked —
    rather than acking away a real job."""

    def test_parsing_an_unknown_queue_raises_a_config_error(self):
        with pytest.raises(BrokerConfigError):
            _parse('sm.scraping.tiktok.re.post', b'{}')

    def test_it_is_not_a_value_error_so_the_ack_policy_cannot_ack_it(self):
        assert not issubclass(BrokerConfigError, ValueError)
        assert not issubclass(BrokerConfigError, MalformedMessage)

    def test_a_delivery_on_an_unhandled_queue_leaves_the_message_unacked(self):
        channel = FakeChannel([('sm.scraping.tiktok.unknown', b'{}')])
        connection = FakeConnection(channel)
        consumer = BrokerConsumer(SETTINGS, FakeApi(), config=ConsumerConfig(queues=('sm.scraping.tiktok.unknown',)), connect=lambda _s: connection)
        with pytest.raises(BrokerConfigError):
            consumer.run()
        assert channel.acked == [] and channel.nacked == []
        assert connection.closed == 1, 'closing requeues the in-flight job'


# ========================================================= 5: --max-messages
class TestMaxMessagesStopsAfterN:
    """How the first live run processes exactly one message before a
    441-message backlog: a wrong envelope shape published 441 times cannot be
    retracted."""

    def deliveries(self, count: int) -> list[tuple]:
        return [(KEYWORD_QUEUE, keyword_body()) for _ in range(count)]

    def test_it_stops_after_the_configured_count(self):
        api = FakeApi(search=[search_payload(1) for _ in range(5)])
        consumer, channel, _ = run_consumer(self.deliveries(5), api, config=ConsumerConfig(max_messages=2, short_backoff_s=SHORT, long_backoff_s=LONG))
        assert consumer.processed == 2
        assert channel.acked == [1, 2]
        assert channel.stopped == 1

    def test_one_means_one(self):
        api = FakeApi(search=[search_payload(1) for _ in range(5)])
        consumer, channel, _ = run_consumer(self.deliveries(5), api, config=ConsumerConfig(max_messages=1))
        assert consumer.processed == 1
        assert len(channel.published) == 1

    def test_none_runs_through_every_delivery(self):
        api = FakeApi(search=[search_payload(1) for _ in range(5)])
        consumer, channel, _ = run_consumer(self.deliveries(5), api, config=ConsumerConfig(max_messages=None))
        assert consumer.processed == 5
        assert channel.stopped == 0
        assert ConsumerConfig().max_messages is None, 'unlimited by default'

    def test_a_dropped_message_still_counts_against_the_limit(self):
        # "Messages taken off a queue so far, whatever the outcome" — otherwise
        # `--max-messages 1` over a malformed backlog would never stop.
        consumer, channel, _ = run_consumer([(KEYWORD_QUEUE, b'{}'), (KEYWORD_QUEUE, keyword_body())], FakeApi(), config=ConsumerConfig(max_messages=1))
        assert consumer.processed == 1
        assert channel.acked == [1]

    def test_run_returns_the_processed_count(self):
        channel = FakeChannel(self.deliveries(3))
        connection = FakeConnection(channel)
        api = FakeApi(search=[search_payload(1) for _ in range(3)])
        consumer = BrokerConsumer(SETTINGS, api, config=ConsumerConfig(max_messages=2), connect=lambda _s: connection)
        assert consumer.run() == 2


class TestTheWorkerCli:
    """`mobile/worker.py`'s argument surface.

    `main()` is deliberately NOT called: it resolves `broker_settings()` with
    no arguments, which reads the repo-root `.env` — the file that holds the
    live broker password."""

    def parser(self):
        import worker
        return worker.build_parser()

    def test_max_messages_is_parsed_as_an_int(self):
        assert self.parser().parse_args(['--max-messages', '2']).max_messages == 2

    def test_max_messages_defaults_to_unlimited(self):
        assert self.parser().parse_args([]).max_messages is None

    @pytest.mark.parametrize('raw', ['0', '-1'])
    def test_a_non_positive_max_messages_is_rejected(self, raw):
        import worker
        with pytest.raises(argparse.ArgumentTypeError):
            worker.positive_int(raw)

    def test_the_parsed_flag_actually_stops_the_consumer(self):
        # The flag and the behaviour, wired the way `main` wires them, without
        # calling `main` (which would read the real `.env`).
        args = self.parser().parse_args(['--max-messages', '2', '--queues', 'keyword'])
        config = ConsumerConfig(queues=QUEUE_SELECTIONS[args.queues], prefetch=args.prefetch, max_messages=args.max_messages)
        api = FakeApi(search=[search_payload(1) for _ in range(4)])
        consumer, channel, _ = run_consumer([(KEYWORD_QUEUE, keyword_body())] * 4, api, config=config)
        assert consumer.processed == 2
        assert channel.consume_order == [KEYWORD_QUEUE]

    @pytest.mark.parametrize('selection,expected', [('keyword', (KEYWORD_QUEUE,)), ('page', (PAGE_QUEUE,)), ('both', (KEYWORD_QUEUE, PAGE_QUEUE))])
    def test_the_queue_selections_are_the_two_job_queues(self, selection, expected):
        assert QUEUE_SELECTIONS[selection] == expected

    def test_both_is_the_default_selection(self):
        assert self.parser().parse_args([]).queues == 'both'

    def test_prefetch_defaults_to_one(self):
        assert self.parser().parse_args([]).prefetch == DEFAULT_PREFETCH == 1


# ================================================ 6: the API classification
NO_JSON = object()


class Response:
    """`requests.Response`, reduced to the two members `_failure` reads."""

    def __init__(self, status: int, payload=NO_JSON) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is NO_JSON:
            raise ValueError('not json')
        return self._payload


class FakeSession:
    """The HTTP boundary. Records the request and answers from a script; a
    scripted exception is raised instead. No socket is ever opened."""

    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[dict] = []
        self.closed = 0

    def post(self, url, json=None, timeout=None):  # noqa: ANN001, A002
        self.calls.append({'url': url, 'json': json, 'timeout': timeout})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def close(self) -> None:
        self.closed += 1


def client(answer, base_url: str = 'http://127.0.0.1:8000') -> tuple[ApiClient, FakeSession]:
    session = FakeSession(answer)
    return ApiClient(base_url, session=session), session


def failure_of(status: int, payload=NO_JSON) -> ApiCallError:
    api, _ = client(Response(status, payload))
    with pytest.raises(ApiCallError) as caught:
        api.search(query='q', limit=1)
    return caught.value


class TestTheApiRequests:
    def test_a_keyword_search_posts_the_keyword_type_query_and_limit(self):
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.search(query='Şəki', limit=30)
        assert session.calls[0]['url'] == 'http://127.0.0.1:8000/search'
        assert session.calls[0]['json'] == {'type': 'keyword', 'query': 'Şəki', 'limit': 30}

    def test_filters_are_omitted_entirely_when_the_job_carried_none(self):
        # An empty `filters` object is not the same request as no `filters` at
        # all, and a null filter means "no filter", never a default one.
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.search(query='q', limit=1, sort_type=None, publish_time=None)
        assert 'filters' not in session.calls[0]['json']

    def test_filters_are_sent_as_their_wire_values_when_present(self):
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.search(query='q', limit=1, sort_type=SortType.MOST_LIKED, publish_time=PublishTime.LAST_MONTH)
        assert session.calls[0]['json']['filters'] == {'sort_type': '1', 'publish_time': '30'}

    def test_only_the_filter_that_was_set_is_sent(self):
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.search(query='q', limit=1, sort_type=SortType.RELEVANCE)
        assert session.calls[0]['json']['filters'] == {'sort_type': '0'}

    def test_the_profile_call_posts_only_the_username(self):
        api, session = client(Response(200, {'username': HANDLE}))
        api.profile(username=HANDLE)
        assert session.calls[0]['url'].endswith('/profile')
        assert session.calls[0]['json'] == {'username': HANDLE}

    def test_the_posts_call_sends_no_period_so_the_endpoint_default_applies(self):
        # Passing one here would pin the window to whatever this module
        # believed the endpoint default was on the day it was written.
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.user_posts(username=HANDLE, limit=50)
        assert session.calls[0]['json'] == {'username': HANDLE, 'limit': 50}
        assert 'period' not in session.calls[0]['json']

    def test_a_trailing_slash_on_the_base_url_does_not_double_up(self):
        api, session = client(Response(200, {RESULTS_KEY: []}), base_url='http://127.0.0.1:8000/')
        api.search(query='q', limit=1)
        assert session.calls[0]['url'] == 'http://127.0.0.1:8000/search'

    def test_both_timeouts_are_sent(self):
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.search(query='q', limit=1)
        connect, read = session.calls[0]['timeout']
        assert connect < read, 'fail fast on connect, be patient on read'

    def test_close_closes_the_session(self):
        api, session = client(Response(200, {RESULTS_KEY: []}))
        api.close()
        assert session.closed == 1


class TestTheFailureClassification:
    """The ack decision is made on this classification and never on the status
    code or the message prose — the same discipline as `errors.PoolCode`."""

    @pytest.mark.parametrize('status', [400, 401, 403, 404, 405, 409, 410, 422])
    def test_a_client_error_is_permanent(self, status):
        assert failure_of(status, {'detail': 'no such user'}).failure is Failure.PERMANENT

    @pytest.mark.parametrize('status', [500, 502, 503, 504])
    def test_a_server_error_is_transient(self, status):
        assert failure_of(status, {'detail': 'pool busy'}).failure is Failure.TRANSIENT

    def test_a_408_is_transient_because_it_says_try_again(self):
        assert failure_of(408).failure is Failure.TRANSIENT

    def test_the_status_is_carried_on_the_error(self):
        assert failure_of(404, {'detail': 'no such user'}).status == 404

    def test_an_unreachable_api_is_transient(self):
        # The plan's "API unreachable" row: the job is requeued rather than
        # dropped while the server is restarting.
        api, _ = client(requests.ConnectionError('refused'))
        with pytest.raises(ApiCallError) as caught:
            api.search(query='q', limit=1)
        assert caught.value.failure is Failure.TRANSIENT

    def test_an_unreachable_api_reports_the_class_and_not_the_url(self):
        # `requests`' own text can carry the full URL and any proxy in play.
        api, _ = client(requests.ConnectionError('HTTPConnectionPool(host=secret-host, port=1)'))
        with pytest.raises(ApiCallError) as caught:
            api.search(query='q', limit=1)
        assert 'ConnectionError' in str(caught.value)
        assert 'secret-host' not in str(caught.value)

    @pytest.mark.parametrize('exc', [requests.Timeout(), requests.ConnectionError(), requests.TooManyRedirects()])
    def test_every_request_exception_is_transient(self, exc):
        api, _ = client(exc)
        with pytest.raises(ApiCallError) as caught:
            api.search(query='q', limit=1)
        assert caught.value.failure is Failure.TRANSIENT

    def test_a_2xx_that_is_not_json_is_transient(self):
        # Not something a redelivery can fix by itself, but not the producer's
        # fault either: the likeliest cause is something OTHER than our API
        # answering on that port.
        api, _ = client(Response(200))
        with pytest.raises(ApiCallError) as caught:
            api.search(query='q', limit=1)
        assert caught.value.failure is Failure.TRANSIENT

    @pytest.mark.parametrize('payload', [[], 'a string', 7, None])
    def test_a_2xx_whose_json_is_not_an_object_is_transient(self, payload):
        api, _ = client(Response(200, payload))
        with pytest.raises(ApiCallError) as caught:
            api.search(query='q', limit=1)
        assert caught.value.failure is Failure.TRANSIENT


class TestTheTwo429sAreToldApartOnTheDetailPrefix:
    """`api/app.py._domain_errors` maps BOTH `PoolExhausted(PoolCode.CAP)` and
    `RateLimited` to 429, and only the detail prose distinguishes them. The
    prefix is matched, and the FALL-THROUGH is deliberate."""

    def test_the_rate_limit_prose_is_classified_transient(self):
        detail = f'{RATE_LIMITED_DETAIL_PREFIX} us; retry shortly'
        assert failure_of(429, {'detail': detail}).failure is Failure.TRANSIENT

    def test_a_cap_429_is_classified_cap_exhausted(self):
        detail = 'daily request cap reached for every device (300/device)'
        assert failure_of(429, {'detail': detail}).failure is Failure.CAP_EXHAUSTED

    @pytest.mark.parametrize('payload', [NO_JSON, {}, {'detail': None}, {'detail': ''}, {'detail': 'reworded upstream'}, {'detail': ['x']}, []])
    def test_an_unrecognised_429_takes_the_cap_path(self, payload):
        # THE FALL-THROUGH DIRECTION. If that prose is ever reworded, over-
        # waiting is cheap and hammering an exhausted daily cap is exactly what
        # the long backoff exists to prevent. A default of TRANSIENT here would
        # spin against a cap that only resets on a day boundary.
        assert failure_of(429, payload).failure is Failure.CAP_EXHAUSTED

    def test_the_prefix_is_the_literal_the_api_emits(self):
        assert RATE_LIMITED_DETAIL_PREFIX == 'TikTok rate-limited'

    def test_the_prefix_is_matched_at_the_start_and_not_anywhere(self):
        # A cap message that happened to mention the phrase mid-sentence must
        # not be read as a rate-limit and retried in seconds.
        assert failure_of(429, {'detail': f'daily cap reached, not {RATE_LIMITED_DETAIL_PREFIX}'}).failure is Failure.CAP_EXHAUSTED

    def test_the_two_are_different_classifications(self):
        assert Failure.TRANSIENT is not Failure.CAP_EXHAUSTED


class TestTheLoggedDetail:
    def test_a_string_detail_is_kept_and_bounded(self):
        error = failure_of(500, {'detail': 'x' * 5_000})
        assert 'x' * MAX_LOGGED_DETAIL_CHARS in str(error)
        assert len(str(error)) < 5_000
        assert MAX_LOGGED_DETAIL_CHARS == 200

    def test_a_422_list_detail_keeps_only_the_field_locations(self):
        # Every entry of FastAPI's 422 body carries the rejected `input` —
        # which on this path is the producer's own string.
        body = {'detail': [{'loc': ['body', 'username'], 'msg': 'bad', 'input': 'PRODUCER-SUPPLIED-GARBAGE'}]}
        error = failure_of(422, body)
        assert 'body.username' in str(error)
        assert 'PRODUCER-SUPPLIED-GARBAGE' not in str(error)

    def test_a_detail_that_is_neither_string_nor_list_is_dropped(self):
        assert str(failure_of(500, {'detail': {'nested': 'object'}})).endswith('500: ')


class TestTheResultsHelper:
    def test_an_empty_list_is_returned_as_an_empty_list(self):
        assert results({'count': 0, RESULTS_KEY: []}) == []

    def test_a_populated_list_is_returned_verbatim(self):
        records = [post('1'), post('2')]
        assert results({RESULTS_KEY: records}) == records

    @pytest.mark.parametrize('payload', [{}, {'count': 0}, {RESULTS_KEY: None}, {RESULTS_KEY: {}}, {RESULTS_KEY: 'none'}, {RESULTS_KEY: 0}, {'result': []}])
    def test_a_missing_or_non_list_results_key_raises_transient(self, payload):
        with pytest.raises(ApiCallError) as caught:
            results(payload)
        assert caught.value.failure is Failure.TRANSIENT

    def test_the_key_is_the_one_the_api_answers_with(self):
        assert RESULTS_KEY == 'results'

    def test_the_keyword_search_type_comes_from_the_endpoints_own_enum(self):
        # Taken from the enum `POST /search` validates against rather than
        # written out, so the two cannot drift apart.
        assert api_module.KEYWORD_SEARCH_TYPE == 'keyword'


# ============================================== 7: the environment edge
class TestParseEnv:
    """A minimal stdlib `.env` reader, so no `python-dotenv` dependency."""

    def test_a_plain_assignment_is_read(self):
        assert parse_env('RABBITMQ_HOST=fake-broker.invalid') == {'RABBITMQ_HOST': 'fake-broker.invalid'}

    def test_blank_lines_and_comments_are_skipped(self):
        text = '\n# a comment\n\nRABBITMQ_USER=fake-worker-user\n   \n'
        assert parse_env(text) == {'RABBITMQ_USER': 'fake-worker-user'}

    def test_a_commented_out_assignment_stays_commented_out(self):
        # A comment with NO '=' is skipped by the no-separator guard whichever
        # way the comment test is written, so it cannot tell the two apart —
        # only a commented-out ASSIGNMENT can. `.env.example` is full of them.
        assert parse_env('# RABBITMQ_HOST=commented-out.invalid\nRABBITMQ_HOST=fake-broker.invalid') == {'RABBITMQ_HOST': 'fake-broker.invalid'}

    def test_a_commented_out_assignment_alone_yields_nothing(self):
        assert parse_env('# RABBITMQ_PASSWORD=commented-out') == {}

    def test_a_leading_export_is_tolerated(self):
        assert parse_env('export RABBITMQ_USER=fake-worker-user') == {'RABBITMQ_USER': 'fake-worker-user'}

    def test_the_split_is_on_the_first_equals_so_a_value_may_contain_more(self):
        assert parse_env('K=a=b=c') == {'K': 'a=b=c'}

    def test_surrounding_quotes_are_removed(self):
        assert parse_env('A="fake-one"\nB=\'fake-two\'') == {'A': 'fake-one', 'B': 'fake-two'}

    def test_a_hash_inside_a_value_is_not_a_comment(self):
        # An unquoted password may legitimately contain '#', and silently
        # truncating it would produce an authentication failure with no
        # visible cause.
        assert parse_env('RABBITMQ_PASSWORD=not-a-real#password') == {'RABBITMQ_PASSWORD': 'not-a-real#password'}

    def test_a_line_with_no_equals_is_skipped(self):
        assert parse_env('JUST_A_WORD\nK=v') == {'K': 'v'}

    def test_a_line_with_an_empty_key_is_skipped(self):
        assert parse_env('=orphan\nK=v') == {'K': 'v'}

    def test_a_later_line_wins(self):
        assert parse_env('K=first\nK=second') == {'K': 'second'}


class TestReadEnvFile:
    def test_a_missing_file_is_normal_and_reads_as_empty(self):
        # Every key may come from the real environment instead.
        assert read_env_file(Path('/nonexistent') / 'absent.env') == {}

    def test_an_unreadable_file_is_reported_rather_than_treated_as_empty(self, tmp_path):
        # Treating it as empty would surface as "missing credentials" and send
        # the operator looking in the wrong place.
        with pytest.raises(BrokerConfigError):
            read_env_file(tmp_path)

    def test_a_present_file_is_parsed(self, tmp_path):
        path = tmp_path / 'fake.env'
        path.write_text('RABBITMQ_HOST=fake-broker.invalid\n', encoding='utf-8')
        assert read_env_file(path) == {'RABBITMQ_HOST': 'fake-broker.invalid'}


class TestBrokerSettings:
    """Every case passes an explicit `environ` AND an `env_path` under
    `tmp_path`. The repo-root `.env` holds the live broker password and is
    never read here, nor is any real value ever asserted on."""

    ENVIRON = {'RABBITMQ_HOST': FAKE_HOST, 'RABBITMQ_USER': FAKE_USER, 'RABBITMQ_PASSWORD': FAKE_PASSWORD}

    def absent(self, tmp_path) -> Path:
        return tmp_path / 'absent.env'

    def test_the_three_required_keys_resolve_from_the_environment(self, tmp_path):
        settings = broker_settings(env_path=self.absent(tmp_path), environ=self.ENVIRON)
        assert (settings.host, settings.user) == (FAKE_HOST, FAKE_USER)

    def test_the_optional_keys_take_their_defaults(self, tmp_path):
        settings = broker_settings(env_path=self.absent(tmp_path), environ=self.ENVIRON)
        assert (settings.port, settings.vhost, settings.exchange) == (5672, '/', 'sm.scraping.tiktok')
        assert (DEFAULT_PORT, DEFAULT_VHOST, DEFAULT_EXCHANGE) == (5672, '/', 'sm.scraping.tiktok')

    @pytest.mark.parametrize('missing', ['RABBITMQ_HOST', 'RABBITMQ_USER', 'RABBITMQ_PASSWORD'])
    def test_a_missing_required_key_is_a_config_error_naming_it(self, tmp_path, missing):
        # Guessing localhost/guest would turn a missing credential into a
        # confusing connection refusal instead of a clear start-up error.
        environ = {key: value for key, value in self.ENVIRON.items() if key != missing}
        with pytest.raises(BrokerConfigError) as caught:
            broker_settings(env_path=self.absent(tmp_path), environ=environ)
        assert missing in str(caught.value)

    def test_nothing_falls_back_to_the_repos_own_env_file(self, tmp_path):
        # THE GUARD FOR THIS WHOLE CLASS. With an empty environ and an absent
        # env file, all three keys must be reported missing — if the real
        # repo-root `.env` were consulted, they would resolve and this would
        # not raise.
        with pytest.raises(BrokerConfigError) as caught:
            broker_settings(env_path=self.absent(tmp_path), environ={})
        for key in ('RABBITMQ_HOST', 'RABBITMQ_USER', 'RABBITMQ_PASSWORD'):
            assert key in str(caught.value)
        assert 'absent.env' in str(caught.value)

    def test_a_key_resolves_from_the_file_when_the_environment_lacks_it(self, tmp_path):
        path = tmp_path / 'fake.env'
        path.write_text(f'RABBITMQ_HOST={FAKE_HOST}\nRABBITMQ_USER={FAKE_USER}\nRABBITMQ_PASSWORD={FAKE_PASSWORD}\n', encoding='utf-8')
        settings = broker_settings(env_path=path, environ={})
        assert settings.host == FAKE_HOST

    def test_the_environment_wins_over_the_file(self, tmp_path):
        # What lets an operator override one key for one run without editing
        # the file that holds the password.
        path = tmp_path / 'fake.env'
        path.write_text('RABBITMQ_HOST=from-the-file.invalid\nRABBITMQ_USER=file-user\nRABBITMQ_PASSWORD=file-password\n', encoding='utf-8')
        settings = broker_settings(env_path=path, environ={'RABBITMQ_HOST': 'from-the-environment.invalid'})
        assert settings.host == 'from-the-environment.invalid'
        assert settings.user == 'file-user', 'the other keys still come from the file'

    def complete_file(self, tmp_path) -> Path:
        path = tmp_path / 'fake.env'
        path.write_text(f'RABBITMQ_HOST={FAKE_HOST}\nRABBITMQ_USER={FAKE_USER}\nRABBITMQ_PASSWORD={FAKE_PASSWORD}\n', encoding='utf-8')
        return path

    def test_an_empty_environment_value_falls_through_to_the_file(self, tmp_path):
        # MEASURED: `''` is falsy, so the `or` reaches the file.
        settings = broker_settings(env_path=self.complete_file(tmp_path), environ={'RABBITMQ_HOST': ''})
        assert settings.host == FAKE_HOST

    def test_a_whitespace_only_environment_value_is_reported_missing(self, tmp_path):
        # MEASURED, and the OTHER direction from the empty string above: a
        # whitespace-only value is truthy, so it wins the `or` and the file is
        # never consulted — then the strip empties it and the key is reported
        # missing. Loud and clear at start-up rather than a silent fallback to
        # a different credential than the operator set, so it is pinned as it
        # stands; the two spellings taking different paths is the point.
        with pytest.raises(BrokerConfigError) as caught:
            broker_settings(env_path=self.complete_file(tmp_path), environ={'RABBITMQ_HOST': '   '})
        assert 'RABBITMQ_HOST' in str(caught.value)

    def test_the_port_is_coerced_to_an_int(self, tmp_path):
        settings = broker_settings(env_path=self.absent(tmp_path), environ={**self.ENVIRON, 'RABBITMQ_PORT': '5673'})
        assert settings.port == 5673 and isinstance(settings.port, int)

    def test_a_non_numeric_port_names_the_key_and_not_the_value(self, tmp_path):
        # A mis-set port is still operator data.
        with pytest.raises(BrokerConfigError) as caught:
            broker_settings(env_path=self.absent(tmp_path), environ={**self.ENVIRON, 'RABBITMQ_PORT': 'not-a-port'})
        assert 'RABBITMQ_PORT' in str(caught.value)
        assert 'not-a-port' not in str(caught.value)

    @pytest.mark.parametrize('port', ['0', '-1', '65536', '99999'])
    def test_a_port_outside_the_valid_range_is_refused(self, tmp_path, port):
        with pytest.raises(BrokerConfigError) as caught:
            broker_settings(env_path=self.absent(tmp_path), environ={**self.ENVIRON, 'RABBITMQ_PORT': port})
        assert 'RABBITMQ_PORT' in str(caught.value)

    def test_the_exchange_is_overridable_because_it_is_deployment_data(self, tmp_path):
        settings = broker_settings(env_path=self.absent(tmp_path), environ={**self.ENVIRON, 'RABBITMQ_EXCHANGE': SENTINEL_EXCHANGE})
        assert settings.exchange == SENTINEL_EXCHANGE

    def test_the_password_never_appears_in_a_repr(self, tmp_path):
        # A dataclass `__repr__` prints every field, so the default would put
        # the live broker password into any log line, traceback or debugger
        # frame that rendered this object.
        settings = broker_settings(env_path=self.absent(tmp_path), environ=self.ENVIRON)
        assert FAKE_PASSWORD not in repr(settings)
        assert FAKE_HOST in repr(settings), 'the non-secret fields still render'

    def test_no_value_is_logged_when_the_settings_resolve(self, tmp_path, caplog):
        caplog.set_level('DEBUG', logger='tiktoksearch.broker.env')
        broker_settings(env_path=self.absent(tmp_path), environ=self.ENVIRON)
        assert FAKE_PASSWORD not in caplog.text
        assert FAKE_USER not in caplog.text

    def test_the_settings_object_is_frozen(self, tmp_path):
        settings = broker_settings(env_path=self.absent(tmp_path), environ=self.ENVIRON)
        with pytest.raises(Exception):
            settings.host = 'mutated.invalid'


class TestTheDefaultEnvPath:
    """Resolved from the module file and NOT from the process cwd — the same
    lesson as `api/app.py._resolve_identities_path`, where a cwd-relative path
    made the server silently miss `identities.json`."""

    def test_it_is_an_absolute_path_named_env(self):
        assert DEFAULT_ENV_PATH.is_absolute()
        assert DEFAULT_ENV_PATH.name == '.env'

    def test_it_points_at_the_repo_root_rather_than_wherever_pytest_ran(self):
        # `mobile/` sits beside it, which is what makes this the repo root and
        # not the `mobile` cwd these tests run from.
        assert (DEFAULT_ENV_PATH.parent / 'mobile' / 'worker.py').is_file()
        assert DEFAULT_ENV_PATH.parent != Path.cwd()

    def test_the_real_file_is_not_what_these_tests_resolve(self, tmp_path):
        # Belt and braces on the guard in `TestBrokerSettings`: an env_path
        # under `tmp_path` is not the repo-root `.env`, so nothing in this
        # module can resolve a live credential even by accident.
        assert (tmp_path / 'absent.env') != DEFAULT_ENV_PATH
        with pytest.raises(BrokerConfigError):
            broker_settings(env_path=tmp_path / 'absent.env', environ={})


class TestTheEnvExampleCarriesNoValues:
    """`.env.example` is COMMITTED, so a value in it would be a leaked
    credential."""

    def test_every_rabbitmq_key_is_present_and_empty(self):
        path = Path(env_module.__file__).resolve().parents[3] / '.env.example'
        lines = [line for line in path.read_text(encoding='utf-8').splitlines() if line.startswith('RABBITMQ_')]
        assert {line.split('=', 1)[0] for line in lines} == {'RABBITMQ_HOST', 'RABBITMQ_PORT', 'RABBITMQ_VHOST', 'RABBITMQ_USER', 'RABBITMQ_PASSWORD', 'RABBITMQ_EXCHANGE'}
        assert all(line.endswith('=') for line in lines), lines
