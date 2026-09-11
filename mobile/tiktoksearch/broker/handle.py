from __future__ import annotations
import logging
import re
from urllib.parse import unquote, urlparse

from ..limits import MAX_USERNAME_CHARS, USERNAME_PATTERN
from .errors import MalformedMessage

logger = logging.getLogger('tiktoksearch.broker.handle')

# The handle this module returns is POSTED to `/profile` and `/user/posts`,
# where `api.schemas._HandleRequest` bounds it at `MAX_USERNAME_CHARS` and
# charset-checks it with `USERNAME_PATTERN`. Both are imported rather than
# restated: a handle accepted here and rejected there would be a job dropped
# on a 422 because one rule was written down twice.
_HANDLE_RE = re.compile(USERNAME_PATTERN)
# A TikTok profile URL is `https://<host>.tiktok.com/@<handle>[/...]`. The
# registrable domain is fixed; the sub-domain is not (`www`, `m`, or bare), so
# the host is matched by suffix rather than against a list of spellings.
_TIKTOK_DOMAIN = 'tiktok.com'
_URL_SCHEMES = ('http', 'https')
# The '@' that marks a handle in a URL PATH (`/@bob`) and, separately, the '@'
# that RFC 3986 uses to end the userinfo of an authority
# (`user:pass@host`). Same character, two unrelated meanings, so two names —
# reusing one for both read as if the host parse were looking for a handle.
_HANDLE_PREFIX = '@'
_USERINFO_SEPARATOR = '@'
# Rejection text. Every rejection leaving this module is one of these two, and
# neither names the offending value: the caller already has the message and
# logs its ids, and the consumer logs a permanent error at WARNING, so
# producer-controlled text must not get in.
_NOT_A_PROFILE_URL = 'page_url is not a TikTok profile URL (expected https://www.tiktok.com/@handle)'
_NO_USABLE_HANDLE = 'no usable TikTok handle in page_url or page_name'


def handle_from_page(page_url: str | None, page_name: str | None) -> str:
    """Return the TikTok handle a page job should be scraped by.

    `page_url` wins when it is present, because it carries the handle TikTok
    itself spells while `page_name` is whatever the producer typed. An absent
    or blank `page_url` falls back to `page_name`.

    A `page_url` that is present but is not a TikTok profile URL raises
    `MalformedMessage` instead of falling back: it means the job disagrees with
    itself about which account it is for, and scraping `page_name` anyway would
    answer a question nobody asked. `MalformedMessage` is a `ValueError`
    subclass and NOT a domain error from `errors.py` (none of which describes a
    bad inbound message), which puts this in the same class as a body that
    would not parse — the consumer's ack policy acks it with a WARNING, since a
    requeue would loop forever.
    """
    if page_url is not None and page_url.strip():
        return _handle_from_url(page_url.strip())
    handle = _normalise(page_name)
    if not _is_valid(handle):
        raise MalformedMessage(_NO_USABLE_HANDLE)
    logger.debug('no page_url, using page_name as the handle: %s', handle)
    return handle


def _handle_from_url(page_url: str) -> str:
    # `urlparse` HAS ITS OWN `ValueError`s, and they are not ours: MEASURED,
    # `https://www.tiktok.com<U+FE6B>evil.com/@x` raises "netloc '...' contains
    # invalid characters under NFKC normalization" (echoing the producer's
    # netloc straight back) and `https://[::1/@bob` raises "Invalid IPv6 URL".
    # Uncaught, both left this module as a rejection whose text we do not own,
    # carrying producer-controlled bytes into a WARNING log line. Caught,
    # re-raised as the named rejection, cause chained with `from` so the real
    # parse error is still there for a debugger and not in the message.
    try:
        parsed = urlparse(page_url)
    except ValueError as exc:
        raise MalformedMessage(_NOT_A_PROFILE_URL) from exc
    if parsed.scheme.lower() not in _URL_SCHEMES:
        raise MalformedMessage(_NOT_A_PROFILE_URL)
    # `netloc` may carry userinfo and a port; the host is what follows the
    # last '@' and precedes the ':'. Taking the LAST '@' segment is what makes
    # `https://www.tiktok.com@evil.com/@x` resolve to `evil.com` and be
    # rejected, as a browser would resolve it.
    host = parsed.netloc.rsplit(_USERINFO_SEPARATOR, 1)[-1].split(':')[0].lower()
    if host != _TIKTOK_DOMAIN and not host.endswith(f'.{_TIKTOK_DOMAIN}'):
        raise MalformedMessage(_NOT_A_PROFILE_URL)
    # Empty segments are dropped, so a trailing slash is not a segment; a
    # query string and a fragment are already off the path. Only the FIRST
    # segment is the handle, which is why `/@handle/video/123` works and
    # `/tag/foo` (no '@' anywhere) does not.
    segments = [segment for segment in parsed.path.split('/') if segment]
    if not segments:
        raise MalformedMessage(_NOT_A_PROFILE_URL)
    # Percent-decoded BEFORE the '@' test, because a browser resolves
    # `https://www.tiktok.com/%40bob` to a real profile and so must we —
    # undecoded it read as a path segment with no '@' and the job was dropped
    # permanently and silently. This widens what we ACCEPT without widening
    # what we FORWARD: the charset and length checks below run on the decoded
    # value, so `%40%C5%9Feki` still decodes to a non-ASCII handle and is still
    # rejected. `unquote` replaces undecodable bytes rather than raising, and
    # U+FFFD fails the charset check like any other character outside it.
    first = unquote(segments[0])
    if not first.startswith(_HANDLE_PREFIX):
        raise MalformedMessage(_NOT_A_PROFILE_URL)
    handle = _normalise(first)
    if not _is_valid(handle):
        raise MalformedMessage(_NOT_A_PROFILE_URL)
    return handle


def _normalise(value: str | None) -> str:
    """Strip surrounding whitespace and exactly one leading `@`.

    Mirrors `api.schemas._HandleRequest._normalise_username`, which is the
    authority: exactly one `@` comes off, so `@@bob` is left failing the
    charset check rather than being guessed at."""
    if not value:
        return ''
    return value.strip().removeprefix(_HANDLE_PREFIX)


def _is_valid(handle: str) -> bool:
    return bool(handle) and len(handle) <= MAX_USERNAME_CHARS and _HANDLE_RE.fullmatch(handle) is not None
