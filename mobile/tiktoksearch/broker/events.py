"""Structured JSONL event log for the worker, read back by `GET /ops/events`.

One JSON object per line, appended with open-append-close per write so another
process (the API server, `tail -f`) can read the file while the worker runs.
The file is rotated to `<path>.1` once it exceeds `max_bytes`.

Never carries credentials: the fields passed in are job bodies, resolved ids
and outbound message bodies — the producer's own data and public post data.
"""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger('tiktoksearch.broker.events')

DEFAULT_EVENTS_DIRNAME = 'data'
DEFAULT_EVENTS_FILENAME = 'worker-events.jsonl'
DEFAULT_MAX_BYTES = 50_000_000
ROTATED_SUFFIX = '.1'
TS_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'


def default_events_path() -> str:
    """`<repo>/data/worker-events.jsonl`, resolved from THIS FILE, never the cwd.

    Same rule as `harvest_spool.default_spool_dir`: the worker that writes and
    the API server that reads must agree on the path wherever each is launched."""
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_root = os.path.dirname(os.path.dirname(package_dir))
    return os.path.join(repo_root, DEFAULT_EVENTS_DIRNAME, DEFAULT_EVENTS_FILENAME)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


class EventLog:
    """Append-only JSONL sink. `emit` never raises: a broken log must not fail a job."""

    def __init__(self, path: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max_bytes
        # Wall-clock nanoseconds, not a per-process counter: two workers (one per
        # queue) append to the SAME file, and the API's `after=<seq>` poll needs
        # one ordering across both writers. Strictly increasing within a process
        # is enforced below so two emits in the same nanosecond cannot tie.
        self._last_seq = 0

    @property
    def path(self) -> str:
        return self._path

    def emit(self, kind: str, **fields: Any) -> None:
        seq = max(time.time_ns(), self._last_seq + 1)
        self._last_seq = seq
        record = {'ts': utc_now_iso(), 'seq': seq, 'kind': kind, **fields}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            self._rotate_if_needed()
            os.makedirs(os.path.dirname(self._path) or '.', exist_ok=True)
            with open(self._path, 'a', encoding='utf-8') as handle:
                handle.write(line + '\n')
        except OSError as exc:
            # Diagnostics must never take a job down with them.
            logger.warning('event log write failed (%s): %s', kind, type(exc).__name__)

    def _rotate_if_needed(self) -> None:
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return
        if size > self._max_bytes:
            os.replace(self._path, self._path + ROTATED_SUFFIX)


def read_events(path: str, *, after: int = 0, limit: int = 200, kind_prefix: str | None = None) -> tuple[list[dict[str, Any]], int, int]:
    """Events with `seq > after`, at most `limit`, in file order.

    Returns `(events, last_seq, size_bytes)`. `last_seq` is the seq of the LAST
    LINE EXAMINED (a filtered-out event advances it too), so a poller passing
    it back as `after` never re-reads or skips a line. A missing file is empty,
    not an error; a partially written last line (no trailing newline) is skipped."""
    lines = _read_lines(path)
    size = events_file_size(path)
    events: list[dict[str, Any]] = []
    last_seq = after
    for line in lines:
        event = _parse(line)
        if event is None:
            continue
        seq = event.get('seq')
        if not isinstance(seq, int) or seq <= after:
            continue
        if len(events) >= limit:
            break
        last_seq = seq
        if kind_prefix and not str(event.get('kind', '')).startswith(kind_prefix):
            continue
        events.append(event)
    return events, last_seq, size


def read_tail(path: str, *, last_n: int) -> list[dict[str, Any]]:
    """The last `last_n` parseable events, in file order."""
    lines = _read_lines(path)[-last_n:]
    return [event for event in (_parse(line) for line in lines) if event is not None]


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as handle:
            text = handle.read()
    except OSError:
        return []
    lines = text.split('\n')
    # Without a trailing newline the last chunk is a write in progress.
    if text and not text.endswith('\n'):
        lines = lines[:-1]
    return [line for line in lines if line]


def events_file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _parse(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None
