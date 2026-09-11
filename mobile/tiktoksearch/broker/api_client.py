"""HTTP calls to the local FastAPI service, classified for the ack policy.

The worker talks to the RUNNING API over localhost rather than importing
`ClientPool`, per the plan: the keyword union, the author filter, the
newest-first ordering, the cap accounting and the `page_token` logic all live
inside the `app.py` handlers, so an in-process worker would have to
reimplement them and there would be two divergent behaviours for one contract.

Every failure leaves here as an `ApiCallError` carrying a `Failure`, because
the consumer's ack decision must be made on a classification and never on
prose or on a bare status code.
"""
from __future__ import annotations
import logging
from enum import Enum
from typing import Any

import requests

from ..filters import PublishTime, SearchKind, SortType

logger = logging.getLogger('tiktoksearch.broker.api_client')

SEARCH_PATH = '/search'
PROFILE_PATH = '/profile'
USER_POSTS_PATH = '/user/posts'
# `POST /search` needs a `type`, and a keyword job is always a keyword search.
# Taken from the enum the endpoint validates against rather than written out,
# so the two cannot drift apart. NOT `messages.SearchType`, which names the
# QUEUE a result came from and happens to spell this one the same way.
KEYWORD_SEARCH_TYPE = SearchKind.KEYWORD.value
RESULTS_KEY = 'results'

# A connect timeout that fails fast (the API is on localhost — either the
# process is up or it is not) and a read timeout that does not: one
# `/user/posts` first page is a resolve plus one pooled search per keyword,
# each of which may spend several signed requests upstream. A read timeout is
# classified TRANSIENT, so a too-short one would nack a job the server was
# still working on and then do it all again.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 300.0

# `api/app.py._domain_errors` maps BOTH `PoolExhausted(PoolCode.CAP)` and
# `RateLimited` to 429, and only the detail prose distinguishes them: the
# `RateLimited` branch answers with this fixed literal, while a cap 429 answers
# with `PoolExhausted.reason`. The plan's ack table requires the two to be told
# apart (a cap 429 needs the LONG backoff, a rate-limit 429 the short one), and
# asking the API to add a machine-readable code would change the response
# contract of endpoints this Epic is not allowed to change.
#
# So: prefix match, with a SAFE fallback. If that prose is ever reworded, an
# unrecognised 429 is treated as the cap case — over-waiting is cheap, and
# hammering an exhausted daily cap is exactly what the long backoff exists to
# prevent.
RATE_LIMITED_DETAIL_PREFIX = 'TikTok rate-limited'
# 4xx is otherwise permanent — a request the API rejects will be rejected
# identically on every redelivery. These two are the exceptions: 429 is split
# above, and 408 says "try again".
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 429})
# Enough of a detail to diagnose, bounded because it lands in a log line.
MAX_LOGGED_DETAIL_CHARS = 200


class Failure(str, Enum):
    """What the consumer must DO about a failed call.

    The ack policy switches on this, never on the status code or the message
    prose — the same discipline as `errors.PoolCode`, and for the same reason:
    rewording a message must not be able to change whether a job is acked."""
    PERMANENT = 'permanent'
    TRANSIENT = 'transient'
    CAP_EXHAUSTED = 'cap_exhausted'


class ApiCallError(Exception):
    """A local-API call that did not produce a usable payload."""

    def __init__(self, failure: Failure, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.failure = failure
        self.status = status


class ApiClient:
    """Thin, synchronous client for the three endpoints a job needs."""

    def __init__(self, base_url: str, *, session: requests.Session | None = None, connect_timeout_s: float = CONNECT_TIMEOUT_S, read_timeout_s: float = READ_TIMEOUT_S) -> None:
        self._base_url = base_url.rstrip('/')
        self._session = requests.Session() if session is None else session
        self._timeout = (connect_timeout_s, read_timeout_s)

    def search(self, *, query: str, limit: int, sort_type: SortType | None = None, publish_time: PublishTime | None = None) -> dict[str, Any]:
        """`POST /search` for a keyword job."""
        body: dict[str, Any] = {'type': KEYWORD_SEARCH_TYPE, 'query': query, 'limit': limit}
        filters = {key: value.value for key, value in (('sort_type', sort_type), ('publish_time', publish_time)) if value is not None}
        if filters:
            # Sent only when the job carried one: an empty `filters` object is
            # not the same request as no `filters` at all, and a null filter
            # means "no filter", never a default one.
            body['filters'] = filters
        return self._post(SEARCH_PATH, body)

    def profile(self, *, username: str) -> dict[str, Any]:
        """`POST /profile` for a page job."""
        return self._post(PROFILE_PATH, {'username': username})

    def user_posts(self, *, username: str, limit: int) -> dict[str, Any]:
        """`POST /user/posts` for a page job.

        `period` is deliberately NOT sent, so the endpoint's own default (30
        days) applies — the plan specifies the endpoint default, and passing a
        value here would pin the window to whatever this module believed the
        default was on the day it was written."""
        return self._post(USER_POSTS_PATH, {'username': username, 'limit': limit})

    def close(self) -> None:
        self._session.close()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(f'{self._base_url}{path}', json=body, timeout=self._timeout)
        except requests.RequestException as exc:
            # The API being down is the plan's "API unreachable" row: transient,
            # so the job is requeued rather than dropped while the server is
            # restarting. The exception CLASS is reported, not its text, which
            # can carry the full URL and any proxy environment in play.
            raise ApiCallError(Failure.TRANSIENT, f'{path} unreachable ({type(exc).__name__})') from exc
        # Path and status only. The request body carries the producer's own
        # keyword, which does not belong in our log lines.
        logger.debug('%s answered %d', path, response.status_code)
        if response.status_code >= 400:
            raise _failure(path, response)
        try:
            payload = response.json()
        except ValueError as exc:
            # A 2xx that is not JSON is not something a redelivery can fix by
            # itself, but it also is not the producer's fault: the likeliest
            # cause is something other than our API answering on that port.
            # Transient, so the job survives until that is corrected.
            raise ApiCallError(Failure.TRANSIENT, f'{path} answered {response.status_code} with a body that is not JSON', status=response.status_code) from exc
        if not isinstance(payload, dict):
            raise ApiCallError(Failure.TRANSIENT, f'{path} answered {response.status_code} with JSON that is not an object', status=response.status_code)
        return payload


def results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The `results` list of a search/posts response.

    An EMPTY list is returned as an empty list: zero posts is a success per the
    plan, and nothing is published for it. A `results` key that is MISSING or
    not a list is not zero posts — it is a response that does not match the
    contract — and raises rather than being read as "found nothing". Reporting
    an empty success for a reply we could not understand is precisely the
    failure `.claude/rules/anti-block.md` exists to forbid."""
    records = payload.get(RESULTS_KEY)
    if not isinstance(records, list):
        raise ApiCallError(Failure.TRANSIENT, f'response carries no `{RESULTS_KEY}` list')
    return records


def _failure(path: str, response: requests.Response) -> ApiCallError:
    status = response.status_code
    detail = _detail(response)
    if status == 429:
        if detail.startswith(RATE_LIMITED_DETAIL_PREFIX):
            return ApiCallError(Failure.TRANSIENT, f'{path} 429 rate-limited: {detail}', status=status)
        return ApiCallError(Failure.CAP_EXHAUSTED, f'{path} 429 daily cap: {detail}', status=status)
    if 400 <= status < 500 and status not in _TRANSIENT_CLIENT_STATUSES:
        return ApiCallError(Failure.PERMANENT, f'{path} {status}: {detail}', status=status)
    return ApiCallError(Failure.TRANSIENT, f'{path} {status}: {detail}', status=status)


def _detail(response: requests.Response) -> str:
    """The response's `detail`, reduced to something safe to log.

    A string `detail` is our own prose from `api/app.py` and is kept, bounded.
    A LIST `detail` is FastAPI's 422 body, and every entry of it carries the
    rejected `input` — which on this path is the producer's own string. Only
    the field locations are kept from those: the shape of that text is not ours
    and a WARNING line is not the place for it."""
    try:
        payload = response.json()
    except ValueError:
        return ''
    if not isinstance(payload, dict):
        return ''
    detail = payload.get('detail')
    if isinstance(detail, str):
        return detail[:MAX_LOGGED_DETAIL_CHARS]
    if isinstance(detail, list):
        locations = [_location(item) for item in detail if isinstance(item, dict)]
        return f"rejected fields: {', '.join(location for location in locations if location)}"[:MAX_LOGGED_DETAIL_CHARS]
    return ''


def _location(error: dict[str, Any]) -> str:
    location = error.get('loc')
    if not isinstance(location, (list, tuple)):
        return ''
    return '.'.join(str(part) for part in location)
