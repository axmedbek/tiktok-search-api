"""RabbitMQ scraping worker: consume jobs, serve them via the local API.

A SEPARATE PROCESS from the API server, which must already be running — the
worker calls `/search`, `/profile` and `/user/posts` over HTTP on localhost
rather than importing `ClientPool`, so it duplicates none of the keyword
union, author filter, ordering or cap accounting those handlers own.

    .venv/bin/python mobile/worker.py --api-url http://127.0.0.1:8000 --queues page --max-messages 1

`--source device` serves PAGE jobs from the genuine TikTok app in the local
Waydroid container instead, reading what `harvest_spool_addon.py` spooled off
the app's own traffic — the `api32-core-alisg` gateway rejects both of our
signers for the account post feed (see `tiktoksearch/device/driver.py`).
Keyword jobs stay on `POST /search` under either source. The DEFAULT is
`search`, so a worker started without the flag behaves exactly as before.

Broker credentials are read from the environment, falling back to the repo-root
`.env` (git-ignored). Nothing here logs one.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
from collections.abc import Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tiktoksearch.broker.api_client import ApiClient
from tiktoksearch.broker.consumer import DEFAULT_PREFETCH, QUEUE_SELECTIONS, BrokerConsumer, ConsumerConfig
from tiktoksearch.broker.device_source import DevicePageSource
from tiktoksearch.broker.env import broker_settings
from tiktoksearch.broker.errors import BrokerConfigError
from tiktoksearch.broker.events import EventLog, default_events_path
from tiktoksearch.device.driver import (ADB_BINARY, DEFAULT_HARVEST_TIMEOUT_S, DEFAULT_INTENT_BACKEND,
                                        DEFAULT_SETTLE_S, INTENT_BACKENDS, DeviceConfig, DeviceDriver)
from tiktoksearch.harvest_spool import default_spool_dir

logger = logging.getLogger('tiktoksearch.worker')

DEFAULT_API_URL = 'http://127.0.0.1:8000'
DEFAULT_QUEUES = 'both'
# Where page jobs get their posts from. `search` is the DEFAULT and is the
# path the worker running in production uses: `POST /profile` +
# `POST /user/posts`, unchanged. `device` swaps ONLY the posts half of a page
# job for the app-driven harvest; keyword jobs are untouched by either value.
SOURCE_SEARCH = 'search'
SOURCE_DEVICE = 'device'
SOURCES = (SOURCE_SEARCH, SOURCE_DEVICE)
DEFAULT_SOURCE = SOURCE_SEARCH
# The device path serves an account's whole feed, pinned posts first; a page
# job wants recent posts, so anything older than this is dropped before the
# newest-first trim. `--source device` only.
DEFAULT_MAX_POST_AGE_DAYS = 90
# Pacing between job starts (`ConsumerConfig.min_job_interval_s`). Non-zero
# here, zero in `ConsumerConfig`: the library default is "no behaviour change",
# the operator default is what the live single-device worker needs.
DEFAULT_MIN_JOB_INTERVAL_S = 15.0
LOG_FORMAT = '%(asctime)s %(levelname)-7s %(name)s | %(message)s'
LOG_DATEFMT = '%H:%M:%S'
LOG_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')
# Exit codes: 0 ran, 2 could not start (missing credentials / bad setting).
EXIT_OK = 0
EXIT_CONFIG = 2


def positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError('must be 1 or more')
    return value


def positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be greater than 0')
    return value


def non_negative_float(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError('must be 0 or more')
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Consume TikTok scraping jobs from RabbitMQ and publish one message per post.')
    parser.add_argument('--api-url', default=DEFAULT_API_URL, help=f'Base URL of the running search API (default: {DEFAULT_API_URL}). The server must be up; while it is not, jobs are requeued rather than dropped.')
    parser.add_argument('--queues', choices=sorted(QUEUE_SELECTIONS), default=DEFAULT_QUEUES, help=f'Which job queues to consume (default: {DEFAULT_QUEUES}).')
    parser.add_argument('--prefetch', type=positive_int, default=DEFAULT_PREFETCH, help=f'Unacked messages to hold at once (default: {DEFAULT_PREFETCH}). Concurrency is out of scope: the daily device cap binds long before throughput does.')
    parser.add_argument('--max-messages', type=positive_int, default=None, help='Stop after processing this many messages (default: run until interrupted). Use `--max-messages 1` for a first live run: the backlog is hundreds of jobs and a wrong envelope shape, once published, cannot be retracted.')
    parser.add_argument('--log-level', choices=LOG_LEVELS, default='INFO', help='Root log level (default: INFO).')
    parser.add_argument('--source', choices=SOURCES, default=DEFAULT_SOURCE, help=f'Where PAGE jobs get their posts (default: {DEFAULT_SOURCE}). `search` is `POST /profile` + `POST /user/posts`, unchanged. `device` opens the profile in the TikTok app running in Waydroid and reads what the mitmproxy addon spooled — no signed request, so no daily-cap unit. Keyword jobs stay on `POST /search` under both.')
    parser.add_argument('--spool-dir', default=None, help=f'Directory `harvest_spool_addon.py` writes post-feed entries to (default: {default_spool_dir()}). Only read under `--source device`; pass the SAME directory the addon was given.')
    parser.add_argument('--device-timeout', type=positive_float, default=DEFAULT_HARVEST_TIMEOUT_S, help=f'Seconds to wait for the app to answer one profile visit (default: {DEFAULT_HARVEST_TIMEOUT_S:g}). A timeout is transient: the job is requeued, never acked with zero posts.')
    parser.add_argument('--device-settle', type=positive_float, default=DEFAULT_SETTLE_S, help=f'Seconds of quiet after the last spooled page before a visit is complete (default: {DEFAULT_SETTLE_S:g}). One visit yields two pages a few seconds apart.')
    parser.add_argument('--intent-backend', choices=INTENT_BACKENDS, default=DEFAULT_INTENT_BACKEND, help=f'How the profile intent reaches the app (default: {DEFAULT_INTENT_BACKEND}): the `waydroid` CLI, or `adb shell am start` for an emulator such as LDPlayer.')
    parser.add_argument('--adb-serial', default=None, help='adb device serial, e.g. 127.0.0.1:5555 (default: none, adb picks the only attached device). `--intent-backend adb` only.')
    parser.add_argument('--adb-binary', default=ADB_BINARY, help=f'adb executable (default: {ADB_BINARY}, resolved on PATH).')
    parser.add_argument('--max-post-age-days', type=positive_int, default=DEFAULT_MAX_POST_AGE_DAYS, help=f'Drop device-harvested posts older than this many days, and undated ones (default: {DEFAULT_MAX_POST_AGE_DAYS}). `--source device` only.')
    parser.add_argument('--min-job-interval-s', type=non_negative_float, default=DEFAULT_MIN_JOB_INTERVAL_S, help=f'Minimum seconds between the starts of consecutive jobs (default: {DEFAULT_MIN_JOB_INTERVAL_S:g}; 0 = no pacing). One device on one IP earns a `hit_limit` after roughly 1000 signed searches in two hours; pacing keeps it under.')
    parser.add_argument('--events-path', default=default_events_path(), help=f'JSONL file the operator event log is appended to (default: {default_events_path()}). `GET /ops/events` and `panel.html` read it.')
    parser.add_argument('--no-events', action='store_true', help='Write no event log.')
    return parser


def build_events(args: argparse.Namespace) -> EventLog | None:
    """The operator event log for this run, or None under `--no-events`."""
    if args.no_events:
        return None
    logger.info('event log: %s', args.events_path)
    return EventLog(args.events_path)


def build_page_source(args: argparse.Namespace, api: ApiClient, events: EventLog | None = None) -> DevicePageSource | None:
    """The page-job source for this run, or None to keep today's behaviour.

    None for `--source search`, which is the default — so the search worker
    constructs no driver, opens no spool and imports nothing it did not
    already use."""
    if args.source != SOURCE_DEVICE:
        return None
    spool_dir = args.spool_dir or default_spool_dir()
    driver = DeviceDriver(DeviceConfig(spool_dir=spool_dir, harvest_timeout_s=args.device_timeout,
                                       settle_s=args.device_settle, intent_backend=args.intent_backend,
                                       adb_binary=args.adb_binary, adb_serial=args.adb_serial))
    logger.info('page jobs served from the device (backend=%s, spool=%s, timeout=%gs, settle=%gs, max_post_age_days=%d); keyword jobs stay on /search',
                args.intent_backend, spool_dir, args.device_timeout, args.device_settle, args.max_post_age_days)
    return DevicePageSource(api, driver, max_age_days=args.max_post_age_days, events=events)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    try:
        settings = broker_settings()
    except BrokerConfigError as exc:
        # The message names missing KEYS, never values.
        logger.error('cannot start: %s', exc)
        return EXIT_CONFIG
    config = ConsumerConfig(queues=QUEUE_SELECTIONS[args.queues], prefetch=args.prefetch, max_messages=args.max_messages, min_job_interval_s=args.min_job_interval_s)
    api = ApiClient(args.api_url)
    events = build_events(args)
    try:
        page_source = build_page_source(args, api, events)
    except ValueError as exc:
        # `DeviceConfig` validates its own knobs. A bad one is our wiring, not
        # a job, so it is reported and the process exits without consuming
        # anything. Names the setting, never a credential.
        logger.error('cannot start: %s', exc)
        api.close()
        return EXIT_CONFIG
    consumer = BrokerConsumer(settings, api, config=config, page_source=page_source, events=events)
    try:
        processed = consumer.run()
    except KeyboardInterrupt:
        # `run`'s own `finally` has already closed the connection, which
        # requeues an in-flight job rather than losing it.
        logger.info('interrupted after %d message(s)', consumer.processed)
        return EXIT_OK
    finally:
        api.close()
    logger.info('processed %d message(s)', processed)
    return EXIT_OK


if __name__ == '__main__':
    raise SystemExit(main())
