"""pika wiring: consume the two job queues, apply the ack policy, publish.

Declares NOTHING. The exchange and all four queues already exist on the live
broker (`sm.scraping.tiktok`, type direct, durable, routing key equal to the
queue name for each), and a `queue_declare` from here would either fight the
producer's own settings or fail the channel on a mismatch. The worker also
never touches `sm.scraping.tiktok.re.post`, which is not ours.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

import pika
from pika.exceptions import AMQPError
from pydantic import BaseModel, ValidationError

from .api_client import PROFILE_PATH, SEARCH_PATH, USER_POSTS_PATH, ApiCallError, ApiClient, Failure, results
from .env import BrokerSettings
from .envelope import body as envelope_body, keyword_envelopes, page_envelopes, to_json
from .errors import BrokerConfigError, MalformedMessage
from .events import EventLog
from .handle import handle_from_page
from .messages import KeywordMessage, OutboundMessage, PageMessage

logger = logging.getLogger('tiktoksearch.broker.consumer')

# Measured on the live broker. Routing key equals the queue name, so the two
# job queues are also the two routing keys the producer publishes on.
KEYWORD_QUEUE = 'sm.scraping.tiktok.keyword'
PAGE_QUEUE = 'sm.scraping.tiktok.page'
# ALL results go here — keyword and page alike (confirmed by the requester),
# which is why the name says `keyword.result` for both.
RESULT_ROUTING_KEY = 'sm.scraping.tiktok.keyword.result'
RESULT_CONTENT_TYPE = 'application/json'
# Persistent, matching the `delivery_mode: 2` the inbound messages arrive with.
# The literal rather than `pika.DeliveryMode.Persistent` so the value the
# contract names is the value in the code.
DELIVERY_MODE_PERSISTENT = 2

# What `--queues` selects.
QUEUE_SELECTIONS: dict[str, tuple[str, ...]] = {'keyword': (KEYWORD_QUEUE,), 'page': (PAGE_QUEUE,), 'both': (KEYWORD_QUEUE, PAGE_QUEUE)}

# One message at a time. Concurrency is out of scope per the plan: each page
# job spends ~4 device-cap units and the measured cap is 500 units/day, so the
# cap binds long before throughput does.
DEFAULT_PREFETCH = 1
# Long enough that a restarting API or a transient upstream failure is not
# re-attempted in a tight loop.
SHORT_BACKOFF_S = 5.0
# A 503 from the API means "no usable warm identity" — the identity store's
# stale cooldown is measured in minutes, so retrying in seconds only hammers
# the 503 (measured: every job requeued at 5 s for the whole cooldown).
MEDIUM_BACKOFF_S = 120.0
# A 502 (TikTok answered empty / shadow-block) for the SAME job this many times
# is given up on: measured 2026-09-14, `@user12569217` (an auto-generated handle
# TikTok's user search never surfaces) requeued every backoff forever and held
# the page queue for 2 minutes per retry. The counter is per process, so a
# worker restart grants a fresh set of attempts.
SOFT_ERROR_STATUS = 502
# A keyword whose `/search` answers 502 (empty, shadow-block-shaped) is checked
# against a CONTROL query on the same API: measured 2026-09-14, `климатические
# действия` answered empty for an hour while `baku` kept returning 10 records
# on the same identity, so the empty was TikTok having no results, not a block.
# Control OK -> the job is a genuine empty (acked, zero messages); control also
# empty -> a real block, requeued with the medium backoff as before.
CONTROL_QUERY = 'tiktok'
CONTROL_LIMIT = 1
MAX_CONTROL_ERROR_CHARS = 200
MAX_SOFT_ATTEMPTS = 3
# A device harvest timeout (`device_source.DEVICE_TIMEOUT_STATUS`) is counted the
# same way: measured 2026-09-16, one account that never yields a post feed
# otherwise cycles every 50 s forever and trips the supervisor's watchdog into
# restarting the emulator for everybody else.
DEVICE_TIMEOUT_STATUS = 598
GIVE_UP_STATUSES = frozenset((SOFT_ERROR_STATUS, DEVICE_TIMEOUT_STATUS))
NO_IDENTITY_STATUS = 503
# 429 (`RateLimited`, hit_limit) and 502 (SoftError, hit_shark) come from TikTok: a per-device rate or
# risk-control window that lifts with TIME, so retrying in seconds only burns
# signed requests. Both statuses take the medium backoff.
BACKOFF_STATUSES = frozenset((429, 502, NO_IDENTITY_STATUS))
# The daily cap resets on a UTC day boundary, so retrying in seconds is pure
# waste. Chosen well under RabbitMQ's default 30-minute `consumer_timeout`:
# the backoff is spent with the message still UNACKED (see `_nack_after`), and
# exceeding that timeout would have the broker close the channel instead.
LONG_BACKOFF_S = 900.0
# The connection must survive a long backoff, so the heartbeat interval is
# short relative to it and `connection.sleep` keeps answering during the wait.
HEARTBEAT_S = 60
BLOCKED_CONNECTION_TIMEOUT_S = 300.0
# `worker.heartbeat` cadence in the event log; scheduled via
# `connection.call_later`, which pika's blocking loop services for free.
EVENT_HEARTBEAT_S = 30.0
# `worker.start.source` values, mirroring `worker.py`'s `--source`.
SOURCE_LABEL_SEARCH = 'search'
SOURCE_LABEL_DEVICE = 'device'
# `worker.stop.reason` values.
STOP_MAX_MESSAGES = 'max_messages'
STOP_INTERRUPTED = 'interrupted'
STOP_CLOSED = 'closed'

_PARSERS: dict[str, type[BaseModel]] = {KEYWORD_QUEUE: KeywordMessage, PAGE_QUEUE: PageMessage}


@dataclass(frozen=True, slots=True)
class ConsumerConfig:
    """Run-shape knobs. Frozen per `.claude/rules/code-standards.md`."""
    queues: tuple[str, ...] = (KEYWORD_QUEUE, PAGE_QUEUE)
    prefetch: int = DEFAULT_PREFETCH
    # None = run until interrupted. A small number is how the first live run
    # processes exactly one message before the 441-message backlog: a wrong
    # envelope shape published 441 times cannot be retracted.
    max_messages: int | None = None
    short_backoff_s: float = SHORT_BACKOFF_S
    medium_backoff_s: float = MEDIUM_BACKOFF_S
    long_backoff_s: float = LONG_BACKOFF_S
    # Minimum seconds between the STARTS of consecutive jobs; 0 = no pacing.
    # One device, one IP: >1000 signed searches in ~2 h earned a `hit_limit`.
    min_job_interval_s: float = 0.0


def open_connection(settings: BrokerSettings) -> pika.BlockingConnection:
    """Open the AMQP connection. The default `connect` of `BrokerConsumer`."""
    credentials = pika.PlainCredentials(settings.user, settings.password)
    parameters = pika.ConnectionParameters(host=settings.host, port=settings.port, virtual_host=settings.vhost, credentials=credentials, heartbeat=HEARTBEAT_S, blocked_connection_timeout=BLOCKED_CONNECTION_TIMEOUT_S)
    # Host and port only — never the user, never the password.
    logger.info('connecting to broker %s:%d', settings.host, settings.port)
    return pika.BlockingConnection(parameters)


class BrokerConsumer:
    """Consume jobs, serve them via the local API, publish one message per post.

    `connect` is injected rather than called directly so the pika boundary is
    stubbable in a unit test — the same discipline the signer boundary follows.
    No test may reach the live broker (`CLAUDE.md` golden rule 3).

    `page_source` is the same kind of seam, for the device harvest path
    (`worker.py --source device`). DEFAULT None, which is today's behaviour
    exactly: `page` jobs go to `POST /profile` + `POST /user/posts` as they
    always have. When it IS supplied, only `page` jobs are routed through it —
    `keyword` jobs stay on `POST /search` unconditionally, because driving
    search inside the app needs UI text entry and is fragile while opening a
    profile by intent is not. The ack policy below is untouched either way:
    the device source raises the same `ApiCallError` classifications.
    """

    def __init__(self, settings: BrokerSettings, api: ApiClient, *, config: ConsumerConfig | None = None, connect: Callable[[BrokerSettings], Any] | None = None, page_source: Callable[[PageMessage], list[OutboundMessage]] | None = None, events: EventLog | None = None) -> None:
        self._settings = settings
        self._api = api
        self._config = ConsumerConfig() if config is None else config
        self._connect = open_connection if connect is None else connect
        self._page_source = page_source
        self._soft_attempts: dict[str, int] = {}
        # Optional operator event log (`events.py`). None = no events, which
        # is today's behaviour exactly; nothing below depends on it.
        self._events = events
        self._connection: Any = None
        self._processed = 0
        self._stop_reason: str | None = None

    @property
    def processed(self) -> int:
        """Messages taken off a queue so far, whatever the outcome."""
        return self._processed

    def run(self) -> int:
        """Consume until `max_messages` or an interrupt; return the count."""
        connection = self._connect(self._settings)
        self._connection = connection
        try:
            channel = connection.channel()
            # Publisher confirms, so `basic_publish` RAISES when the broker
            # does not accept a message. Without them publishing is
            # fire-and-forget and the plan's safety property — "acked only
            # after every outbound message is published" — would be untrue:
            # a job could be acked while its results went nowhere.
            channel.confirm_delivery()
            channel.basic_qos(prefetch_count=self._config.prefetch)
            for queue in self._config.queues:
                # NO queue_declare / exchange_declare anywhere — see the module
                # docstring. `queue` is bound into the callback rather than read
                # off `method.routing_key`, so which handler runs is decided by
                # what we subscribed to, not by a value on the message.
                channel.basic_consume(queue=queue, on_message_callback=partial(self._on_message, queue=queue))
            logger.info('consuming %s (prefetch=%d, max_messages=%s)', ', '.join(self._config.queues), self._config.prefetch, self._config.max_messages if self._config.max_messages is not None else 'unlimited')
            if self._events is not None:
                self._events.emit('worker.start', queues=list(self._config.queues), source=SOURCE_LABEL_SEARCH if self._page_source is None else SOURCE_LABEL_DEVICE, api_url=self._api.base_url)
                self._schedule_heartbeat(connection)
            channel.start_consuming()
        except KeyboardInterrupt:
            self._stop_reason = STOP_INTERRUPTED
            raise
        except Exception as exc:
            self._stop_reason = type(exc).__name__
            raise
        finally:
            self._emit('worker.stop', reason=self._stop_reason or STOP_CLOSED)
            self._connection = None
            # An in-flight job's message is unacked at this point, so closing
            # requeues it rather than losing it.
            if connection.is_open:
                connection.close()
        return self._processed

    def _emit(self, kind: str, **fields: Any) -> None:
        if self._events is not None:
            self._events.emit(kind, **fields)

    def _schedule_heartbeat(self, connection: Any) -> None:
        # Only with an event log AND a real pika connection: the test fake
        # has no timer API and needs none.
        if self._events is None or not hasattr(connection, 'call_later'):
            return

        def beat() -> None:
            self._emit('worker.heartbeat')
            if self._connection is not None and self._connection.is_open:
                connection.call_later(EVENT_HEARTBEAT_S, beat)
        connection.call_later(EVENT_HEARTBEAT_S, beat)

    def _on_message(self, channel: Any, method: Any, properties: Any, body: bytes, *, queue: str) -> None:
        self._processed += 1
        job_start = time.monotonic()
        tag = method.delivery_tag
        # Ids only, never `keyword_name` / `page_name`: the ids are small
        # integers with a shape we own, they are what the producer can
        # correlate on, and producer-controlled text does not belong in a
        # WARNING line. `queue` alone until the body has parsed.
        label = queue
        try:
            job = _parse(queue, body)
            label = _job_label(queue, job)
            self._emit('job.received', queue=queue, delivery_tag=tag, body=job.model_dump(mode='json'), label=label)
            started = time.monotonic()
            envelopes = self._serve(job, label)
            self._emit('job.served', queue=queue, label=label, posts=len(envelopes), elapsed_s=round(time.monotonic() - started, 3))
            self._publish_all(channel, envelopes, queue=queue, label=label)
        except (MalformedMessage, ValidationError) as exc:
            # ACK. A body that will not parse will not parse on redelivery
            # either, so a requeue would loop forever.
            logger.warning('malformed message on %s, acked and dropped: %s', label, _safe_reason(exc))
            self._emit('job.malformed', queue=queue, error=_safe_reason(exc))
            channel.basic_ack(delivery_tag=tag)
        except ApiCallError as exc:
            self._on_api_failure(channel, tag, queue, label, exc)
        except AMQPError as exc:
            # A publish failure, or the connection going away mid-job. Both of
            # pika's confirm failures are `AMQPError` subclasses (verified:
            # `UnroutableError` and `NackError` both inherit `AMQPChannelError`
            # -> `AMQPError`). The inbound message is deliberately NOT acked,
            # so a job that published only some of its posts is redelivered
            # whole rather than half-delivered.
            logger.warning('AMQP failure on %s, requeued: %s', label, type(exc).__name__)
            self._emit('job.requeued', queue=queue, label=label, reason=f'amqp: {type(exc).__name__}', retry_in_s=self._config.short_backoff_s)
            self._nack_after(channel, tag, self._config.short_backoff_s)
        else:
            # Zero posts is a SUCCESS: nothing published, message acked, one
            # INFO line. "One message per post" means zero posts is zero
            # messages, not an error.
            logger.info('%s served: %d result message(s) published', label, len(envelopes))
            self._soft_attempts.pop(label, None)
            self._emit('job.acked', queue=queue, label=label, published=len(envelopes))
            channel.basic_ack(delivery_tag=tag)
        self._stop_if_done(channel)
        if self._stop_reason is None:
            self._pace(job_start)

    def _pace(self, job_start: float) -> None:
        # Sleep only the REMAINDER: a job that already took longer than the
        # interval waits nothing. `connection.sleep`, for the same heartbeat
        # reason as `_nack_after`.
        remaining = self._config.min_job_interval_s - (time.monotonic() - job_start)
        if remaining <= 0 or self._connection is None:
            return
        self._emit('job.paced', seconds=round(remaining, 3))
        self._connection.sleep(remaining)

    def _on_api_failure(self, channel: Any, tag: int, queue: str, label: str, exc: ApiCallError) -> None:
        # Switched on the CLASSIFICATION, never on the bare status: a cap 429
        # and a rate-limit 429 are the same status and need different backoffs
        # (see `api_client.RATE_LIMITED_DETAIL_PREFIX`).
        if exc.failure is Failure.PERMANENT:
            logger.warning('permanent error on %s, acked and dropped: %s', label, exc)
            self._emit('job.failed', queue=queue, label=label, error=str(exc))
            channel.basic_ack(delivery_tag=tag)
            return
        if exc.failure is Failure.CAP_EXHAUSTED:
            logger.warning('daily cap exhausted on %s, requeued after a long backoff: %s', label, exc)
            self._emit('job.requeued', queue=queue, label=label, reason=str(exc), retry_in_s=self._config.long_backoff_s)
            self._nack_after(channel, tag, self._config.long_backoff_s)
            return
        # The one status-based branch: a 503 is the API saying every warm
        # identity is stale, and that clears on the identity cooldown (minutes),
        # so the short backoff would only hammer it.
        if exc.status in GIVE_UP_STATUSES:
            attempts = self._soft_attempts.get(label, 0) + 1
            self._soft_attempts[label] = attempts
            if attempts >= MAX_SOFT_ATTEMPTS:
                self._soft_attempts.pop(label, None)
                logger.warning('giving up on %s after %d empty answers, acked and dropped: %s', label, attempts, exc)
                self._emit('job.failed', queue=queue, label=label, error=f'{exc} (gave up after {attempts} attempts)')
                channel.basic_ack(delivery_tag=tag)
                return
        backoff = self._config.medium_backoff_s if exc.status in BACKOFF_STATUSES else self._config.short_backoff_s
        logger.warning('transient error on %s, requeued in %gs: %s', label, backoff, exc)
        self._emit('job.requeued', queue=queue, label=label, reason=str(exc), retry_in_s=backoff)
        self._nack_after(channel, tag, backoff)

    def _control_search_ok(self, label: str) -> bool:
        """Whether a known-good keyword still returns records right now."""
        self._emit('search.request', label=label, endpoint=SEARCH_PATH, body={'query': CONTROL_QUERY, 'limit': CONTROL_LIMIT, 'control': True})
        try:
            payload = self._api.search(query=CONTROL_QUERY, limit=CONTROL_LIMIT)
        except ApiCallError as exc:
            logger.warning('control query failed too (%s): treating the empty as a real block', exc)
            self._emit('search.response', label=label, endpoint=SEARCH_PATH, count=0, has_more=None, control=True, error=str(exc)[:MAX_CONTROL_ERROR_CHARS])
            return False
        count = len(results(payload))
        self._emit('search.response', label=label, endpoint=SEARCH_PATH, count=count, has_more=payload.get('has_more'), control=True)
        return count > 0

    def _serve(self, job: BaseModel, label: str) -> list[OutboundMessage]:
        if isinstance(job, KeywordMessage):
            # BEFORE the page branch and with no reference to `_page_source`:
            # a keyword job is served by `POST /search` whatever the worker's
            # `--source` is. That is the plan's deliberate division of labour,
            # not an oversight.
            self._emit('search.request', label=label, endpoint=SEARCH_PATH, body={'query': job.keyword_name, 'limit': job.max_results, 'sort_type': _enum_value(job.sort_type), 'publish_time': _enum_value(job.publish_time)})
            try:
                payload = self._api.search(query=job.keyword_name, limit=job.max_results, sort_type=job.sort_type, publish_time=job.publish_time)
            except ApiCallError as exc:
                if exc.status != SOFT_ERROR_STATUS or not self._control_search_ok(label):
                    raise
                logger.info('%s answered empty while the control query succeeded: no results for this keyword, acked with 0 messages', label)
                self._emit('search.response', label=label, endpoint=SEARCH_PATH, count=0, has_more=False, note='empty on a healthy identity (control query succeeded)')
                return []
            records = results(payload)
            self._emit('search.response', label=label, endpoint=SEARCH_PATH, count=len(records), has_more=payload.get('has_more'))
            return keyword_envelopes(job, records)
        if isinstance(job, PageMessage):
            if self._page_source is not None:
                # `--source device`. The source does its own handle resolve
                # (it needs `POST /profile` for the numeric user_id anyway) and
                # raises the same `MalformedMessage` / `ApiCallError` classes,
                # so the ack policy above applies unchanged.
                return self._page_source(job)
            # Raises `MalformedMessage` when `page_url` is present but is not a
            # TikTok profile URL — acked, per the policy above.
            handle = handle_from_page(job.page_url, job.page_name)
            # Two calls, profile first: a page result carries the profile on
            # every message, so there is nothing to publish without it.
            self._emit('search.request', label=label, endpoint=PROFILE_PATH, body={'username': handle})
            profile = self._api.profile(username=handle)
            self._emit('search.response', label=label, endpoint=PROFILE_PATH, count=1, has_more=False)
            self._emit('page.resolved', label=label, handle=handle, user_id=profile.get('user_id'), sec_uid=profile.get('sec_uid'), username=profile.get('username'))
            self._emit('search.request', label=label, endpoint=USER_POSTS_PATH, body={'username': handle, 'limit': job.max_posts})
            payload = self._api.user_posts(username=handle, limit=job.max_posts)
            records = results(payload)
            self._emit('search.response', label=label, endpoint=USER_POSTS_PATH, count=len(records), has_more=payload.get('has_more'))
            return page_envelopes(job, records, profile)
        raise BrokerConfigError(f'no handler for parsed job type {type(job).__name__}')

    def _publish_all(self, channel: Any, envelopes: list[OutboundMessage], *, queue: str, label: str) -> None:
        properties = pika.BasicProperties(content_type=RESULT_CONTENT_TYPE, delivery_mode=DELIVERY_MODE_PERSISTENT)
        for envelope in envelopes:
            # `mandatory=True` with confirms on, so a routing key nothing is
            # bound to raises instead of being silently discarded by the
            # broker. A loud requeue beats losing every result.
            channel.basic_publish(exchange=self._settings.exchange, routing_key=RESULT_ROUTING_KEY, body=to_json(envelope), properties=properties, mandatory=True)
            if self._events is not None:
                self._events.emit('post.published', queue=queue, label=label, routing_key=RESULT_ROUTING_KEY, post_id=envelope.record.get('id'), post_url=envelope.post_url, body=envelope_body(envelope))

    def _nack_after(self, channel: Any, tag: int, seconds: float) -> None:
        # Sleep BEFORE the nack, not after: while the message is unacked the
        # broker cannot redeliver it, so the backoff is actually observed. Nack
        # first and, at prefetch 1, the same message comes straight back and
        # the wait protects nothing.
        #
        # `connection.sleep` and not `time.sleep`: a BlockingConnection that
        # stops servicing I/O misses its heartbeats and the broker drops it —
        # which a 15-minute cap backoff would guarantee. (At a prefetch above
        # 1 this sleep can dispatch another delivery re-entrantly; prefetch is
        # 1 and concurrency is out of scope per the plan.)
        if seconds > 0 and self._connection is not None:
            self._connection.sleep(seconds)
        channel.basic_nack(delivery_tag=tag, requeue=True)

    def _stop_if_done(self, channel: Any) -> None:
        limit = self._config.max_messages
        if limit is not None and self._processed >= limit:
            logger.info('max-messages reached (%d), stopping', self._processed)
            self._stop_reason = STOP_MAX_MESSAGES
            channel.stop_consuming()


def _enum_value(value: Any) -> Any:
    return getattr(value, 'value', value)


def _parse(queue: str, body: bytes) -> BaseModel:
    model = _PARSERS.get(queue)
    if model is None:
        # Not a malformed message — a subscription this module has no handler
        # for. Raised as a config error so it escapes the ack policy and kills
        # the process with the job still unacked, rather than acking away a
        # real job.
        raise BrokerConfigError(f'no parser for queue {queue}')
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise MalformedMessage('message body is not UTF-8') from exc
    # `model_validate_json` reports invalid JSON as a `ValidationError` too, so
    # a broken body and a body with a bad field arrive as one class.
    return model.model_validate_json(text)


def _job_label(queue: str, job: BaseModel) -> str:
    if isinstance(job, KeywordMessage):
        return f'{queue} keyword_id={job.keyword_id}'
    if isinstance(job, PageMessage):
        return f'{queue} page_id={job.page_id}'
    return queue


def _safe_reason(exc: Exception) -> str:
    """A log-safe reason for a rejected message.

    `ValidationError.errors()` carries the rejected `input` — the producer's
    own strings — so only the FIELD LOCATIONS are taken from it. A
    `MalformedMessage`'s text is always one of our own named rejection strings
    and is safe as it stands."""
    if isinstance(exc, ValidationError):
        # `loc` is EMPTY for a body that is not JSON at all, so the error
        # `type` (e.g. `json_invalid`) stands in — otherwise the whole line
        # read "1 validation error(s) on: " and named nothing.
        locations = sorted({'.'.join(str(part) for part in error['loc']) or str(error['type']) for error in exc.errors(include_url=False)})
        return f"{exc.error_count()} validation error(s) on: {', '.join(locations)}"
    return str(exc)
