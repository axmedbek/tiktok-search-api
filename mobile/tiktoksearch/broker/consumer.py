"""pika wiring: consume the two job queues, apply the ack policy, publish.

Declares NOTHING. The exchange and all four queues already exist on the live
broker (`sm.scraping.tiktok`, type direct, durable, routing key equal to the
queue name for each), and a `queue_declare` from here would either fight the
producer's own settings or fail the channel on a mismatch. The worker also
never touches `sm.scraping.tiktok.re.post`, which is not ours.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

import pika
from pika.exceptions import AMQPError
from pydantic import BaseModel, ValidationError

from .api_client import ApiCallError, ApiClient, Failure, results
from .env import BrokerSettings
from .envelope import keyword_envelopes, page_envelopes, to_json
from .errors import BrokerConfigError, MalformedMessage
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
# The daily cap resets on a UTC day boundary, so retrying in seconds is pure
# waste. Chosen well under RabbitMQ's default 30-minute `consumer_timeout`:
# the backoff is spent with the message still UNACKED (see `_nack_after`), and
# exceeding that timeout would have the broker close the channel instead.
LONG_BACKOFF_S = 900.0
# The connection must survive a long backoff, so the heartbeat interval is
# short relative to it and `connection.sleep` keeps answering during the wait.
HEARTBEAT_S = 60
BLOCKED_CONNECTION_TIMEOUT_S = 300.0

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
    long_backoff_s: float = LONG_BACKOFF_S


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

    def __init__(self, settings: BrokerSettings, api: ApiClient, *, config: ConsumerConfig | None = None, connect: Callable[[BrokerSettings], Any] | None = None, page_source: Callable[[PageMessage], list[OutboundMessage]] | None = None) -> None:
        self._settings = settings
        self._api = api
        self._config = ConsumerConfig() if config is None else config
        self._connect = open_connection if connect is None else connect
        self._page_source = page_source
        self._connection: Any = None
        self._processed = 0

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
            channel.start_consuming()
        finally:
            self._connection = None
            # An in-flight job's message is unacked at this point, so closing
            # requeues it rather than losing it.
            if connection.is_open:
                connection.close()
        return self._processed

    def _on_message(self, channel: Any, method: Any, properties: Any, body: bytes, *, queue: str) -> None:
        self._processed += 1
        tag = method.delivery_tag
        # Ids only, never `keyword_name` / `page_name`: the ids are small
        # integers with a shape we own, they are what the producer can
        # correlate on, and producer-controlled text does not belong in a
        # WARNING line. `queue` alone until the body has parsed.
        label = queue
        try:
            job = _parse(queue, body)
            label = _job_label(queue, job)
            envelopes = self._serve(job)
            self._publish_all(channel, envelopes)
        except (MalformedMessage, ValidationError) as exc:
            # ACK. A body that will not parse will not parse on redelivery
            # either, so a requeue would loop forever.
            logger.warning('malformed message on %s, acked and dropped: %s', label, _safe_reason(exc))
            channel.basic_ack(delivery_tag=tag)
        except ApiCallError as exc:
            self._on_api_failure(channel, tag, label, exc)
        except AMQPError as exc:
            # A publish failure, or the connection going away mid-job. Both of
            # pika's confirm failures are `AMQPError` subclasses (verified:
            # `UnroutableError` and `NackError` both inherit `AMQPChannelError`
            # -> `AMQPError`). The inbound message is deliberately NOT acked,
            # so a job that published only some of its posts is redelivered
            # whole rather than half-delivered.
            logger.warning('AMQP failure on %s, requeued: %s', label, type(exc).__name__)
            self._nack_after(channel, tag, self._config.short_backoff_s)
        else:
            # Zero posts is a SUCCESS: nothing published, message acked, one
            # INFO line. "One message per post" means zero posts is zero
            # messages, not an error.
            logger.info('%s served: %d result message(s) published', label, len(envelopes))
            channel.basic_ack(delivery_tag=tag)
        self._stop_if_done(channel)

    def _on_api_failure(self, channel: Any, tag: int, label: str, exc: ApiCallError) -> None:
        # Switched on the CLASSIFICATION, never on the bare status: a cap 429
        # and a rate-limit 429 are the same status and need different backoffs
        # (see `api_client.RATE_LIMITED_DETAIL_PREFIX`).
        if exc.failure is Failure.PERMANENT:
            logger.warning('permanent error on %s, acked and dropped: %s', label, exc)
            channel.basic_ack(delivery_tag=tag)
            return
        if exc.failure is Failure.CAP_EXHAUSTED:
            logger.warning('daily cap exhausted on %s, requeued after a long backoff: %s', label, exc)
            self._nack_after(channel, tag, self._config.long_backoff_s)
            return
        logger.warning('transient error on %s, requeued: %s', label, exc)
        self._nack_after(channel, tag, self._config.short_backoff_s)

    def _serve(self, job: BaseModel) -> list[OutboundMessage]:
        if isinstance(job, KeywordMessage):
            # BEFORE the page branch and with no reference to `_page_source`:
            # a keyword job is served by `POST /search` whatever the worker's
            # `--source` is. That is the plan's deliberate division of labour,
            # not an oversight.
            payload = self._api.search(query=job.keyword_name, limit=job.max_results, sort_type=job.sort_type, publish_time=job.publish_time)
            return keyword_envelopes(job, results(payload))
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
            profile = self._api.profile(username=handle)
            payload = self._api.user_posts(username=handle, limit=job.max_posts)
            return page_envelopes(job, results(payload), profile)
        raise BrokerConfigError(f'no handler for parsed job type {type(job).__name__}')

    def _publish_all(self, channel: Any, envelopes: list[OutboundMessage]) -> None:
        properties = pika.BasicProperties(content_type=RESULT_CONTENT_TYPE, delivery_mode=DELIVERY_MODE_PERSISTENT)
        for envelope in envelopes:
            # `mandatory=True` with confirms on, so a routing key nothing is
            # bound to raises instead of being silently discarded by the
            # broker. A loud requeue beats losing every result.
            channel.basic_publish(exchange=self._settings.exchange, routing_key=RESULT_ROUTING_KEY, body=to_json(envelope), properties=properties, mandatory=True)

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
            channel.stop_consuming()


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
