"""Unit tests for paging.py — the HMAC-authenticated page_token.

A page_token is caller-supplied input on the path to a SIGNED TikTok URL served
by a live warm identity, so the interesting properties are all negative ones:
what `decode` refuses, and in what ORDER it refuses it. Several tests therefore
assert the exact error identity rather than "it raised ValueError" — the whole
point of checking the HMAC before parsing is invisible to a test that only
asserts the exception class.

The module-private `_tag` / `_b64encode` are used deliberately: forging an
*authentically signed* token with a hostile payload is the only way to reach
the validation code that sits behind the HMAC gate.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch import paging  # noqa: E402
from tiktoksearch.client import (  # noqa: E402
    SEARCH_ITEM_PATH,
    SEARCH_PATHS,
    SEARCH_USER_PATH,
    SEARCH_VIDEO_PATH,
)
from tiktoksearch.filters import PublishTime, SearchFilters, SortType  # noqa: E402
from tiktoksearch.paging import (  # noqa: E402
    MAX_ENDPOINT_CURSOR,
    MAX_ENDPOINTS,
    MAX_PAGE_TOKEN_CHARS,
    MAX_SEARCH_ID_CHARS,
    MAX_SEEN_FINGERPRINTS,
    TOKEN_VERSION,
    EndpointState,
    PageToken,
    SeenWindow,
    decode,
    device_handle,
    encode,
    fingerprint,
    query_hash,
    sanitize_search_id,
)

QUERY_HASH = query_hash('keyword', 'ocean', {})
DEVICE = device_handle('DEVA')

# The four distinguishable rejection reasons. Tests assert on these rather than
# on `ValueError`, because the ORDER of the checks is the property under test.
MALFORMED = paging._MALFORMED
UNTRUSTED = paging._UNTRUSTED
UNSUPPORTED = paging._UNSUPPORTED
QUERY_MISMATCH = paging._QUERY_MISMATCH


def _decode(raw: str, *, expected: str = QUERY_HASH, paths=SEARCH_PATHS) -> PageToken:
    return decode(raw, expected_query_hash=expected, allowed_paths=paths)


def _token(*endpoints: EndpointState, seen: tuple[bytes, ...] = ()) -> PageToken:
    return PageToken(version=TOKEN_VERSION, query_hash=QUERY_HASH,
                     device_handle=DEVICE,
                     endpoints=endpoints or (EndpointState(SEARCH_VIDEO_PATH, 10, 'SID1'),),
                     seen=seen)


def _sign_body(body: str) -> str:
    """`body.tag` with an authentic tag — the token passes the HMAC gate and
    reaches the payload validation behind it."""
    return body + '.' + paging._b64encode(paging._tag(body))


def _sign(payload: object) -> str:
    return _sign_body(paging._b64encode(json.dumps(payload).encode('utf-8')))


def _endpoint(**over) -> dict:
    base = {'p': SEARCH_VIDEO_PATH, 'c': 10, 'sid': 'SID1', 'm': True, 's': True}
    base.update(over)
    return base


def _payload(**over) -> dict:
    base = {'v': TOKEN_VERSION, 'q': QUERY_HASH, 'dev': DEVICE,
            'eps': [_endpoint()], 'seen': ''}
    base.update(over)
    return base


def _reason(raw: str, **kw) -> str:
    with pytest.raises(ValueError) as excinfo:
        _decode(raw, **kw)
    return str(excinfo.value)


class TestTokenRoundTrip:
    def test_every_endpoint_field_survives_the_round_trip(self):
        primary = EndpointState(SEARCH_VIDEO_PATH, 30, 'SID-PRIMARY', True, True)
        secondary = EndpointState(SEARCH_ITEM_PATH, 0, '', False, False)
        got = _decode(encode(_token(primary, secondary)))
        assert got.endpoints == (primary, secondary)
        assert got.version == TOKEN_VERSION
        assert got.query_hash == QUERY_HASH
        assert got.device_handle == DEVICE

    def test_dedup_window_survives_the_round_trip(self):
        seen = (fingerprint('a'), fingerprint('b'), fingerprint('c'))
        assert _decode(encode(_token(seen=seen))).seen == seen

    def test_state_for_finds_the_endpoint_and_none_for_a_path_not_carried(self):
        token = _decode(encode(_token(EndpointState(SEARCH_VIDEO_PATH, 40, 'S'))))
        assert token.state_for(SEARCH_VIDEO_PATH).cursor == 40
        assert token.state_for(SEARCH_USER_PATH) is None


class TestTokenAuthentication:
    """The HMAC gate. `UNTRUSTED` vs `MALFORMED` is the observable difference
    between "we rejected the signature" and "we parsed something broken"."""

    def test_tampered_payload_is_untrusted(self):
        body, tag = encode(_token()).split('.')
        flipped = ('A' if body[5] != 'A' else 'B') + body[6:]
        assert _reason(body[:5] + flipped + '.' + tag) == UNTRUSTED

    def test_tampered_tag_is_untrusted(self):
        body, tag = encode(_token()).split('.')
        flipped = ('A' if tag[0] != 'A' else 'B') + tag[1:]
        assert _reason(body + '.' + flipped) == UNTRUSTED

    def test_token_forged_under_a_foreign_secret_is_untrusted(self):
        body = paging._b64encode(json.dumps(_payload()).encode('utf-8'))
        forged = hmac.new(secrets.token_bytes(32), body.encode('ascii'),
                          hashlib.sha256).digest()[:16]
        assert _reason(body + '.' + paging._b64encode(forged)) == UNTRUSTED

    def test_hmac_is_verified_before_the_body_is_base64_decoded(self):
        # THE ordering test. A body that is not even base64 must be rejected as
        # a signature failure, because nothing decoded it: if the parse ran
        # first this would come back MALFORMED.
        assert _reason('!!!not-base64-at-all!!!.AAAAAAAAAAAAAAAAAAAAAA') == UNTRUSTED

    def test_an_authentic_token_with_unparseable_json_is_malformed(self):
        # The other half of the ordering test: same shape, but the tag is
        # genuine, so the parse DOES run and reports the real problem.
        raw = _sign_body(paging._b64encode(b'{"v": '))
        assert _reason(raw) == MALFORMED

    def test_an_authentic_non_dict_payload_is_malformed(self):
        assert _reason(_sign([TOKEN_VERSION, QUERY_HASH])) == MALFORMED

    def test_garbage_with_no_separator_is_malformed(self):
        assert _reason('not-a-token') == MALFORMED

    def test_empty_halves_are_malformed(self):
        for raw in ('', '.', 'body.', '.tag', 'a.b.c'):
            assert _reason(raw) == MALFORMED, raw

    def test_oversized_raw_is_rejected_before_anything_is_verified(self):
        # Cheap length gate ahead of the HMAC: a megabyte of junk costs nothing.
        assert _reason('x' * (MAX_PAGE_TOKEN_CHARS + 1)) == MALFORMED

    def test_an_undecodable_tag_is_malformed(self):
        # A tag whose length cannot be padded to a base64 block never reaches
        # the comparison.
        body, _ = encode(_token()).split('.')
        assert _reason(body + '.' + 'A') == MALFORMED

    def test_a_junk_tag_is_untrusted(self):
        # base64 decoding is lenient (non-alphabet characters are discarded),
        # so a junk tag decodes to b'' and fails the comparison instead.
        body, _ = encode(_token()).split('.')
        assert _reason(body + '.' + '*' * 8) == UNTRUSTED


class TestTokenVersion:
    def test_missing_version_is_malformed(self):
        payload = _payload()
        del payload['v']
        assert _reason(_sign(payload)) == MALFORMED

    def test_non_int_version_is_malformed(self):
        assert _reason(_sign(_payload(v='1'))) == MALFORMED

    def test_bool_version_is_malformed(self):
        # bool is an int in Python; `True` must not read as version 1.
        assert _reason(_sign(_payload(v=True))) == MALFORMED

    def test_unknown_version_is_reported_as_unsupported_not_malformed(self):
        assert _reason(_sign(_payload(v=TOKEN_VERSION + 1))) == UNSUPPORTED


class TestQueryBinding:
    def test_a_token_from_another_query_is_rejected_as_a_mismatch(self):
        raw = encode(_token())
        assert _reason(raw, expected=query_hash('keyword', 'volcano', {})) == QUERY_MISMATCH

    def test_non_string_query_hash_is_malformed(self):
        assert _reason(_sign(_payload(q=1))) == MALFORMED

    def test_query_hash_is_stable_for_the_same_query(self):
        assert query_hash('keyword', 'ocean', {}) == query_hash('keyword', 'ocean', {})

    def test_query_hash_binds_the_kind(self):
        assert query_hash('keyword', 'ocean', {}) != query_hash('hashtag', 'ocean', {})

    def test_query_hash_binds_the_term(self):
        assert query_hash('keyword', 'ocean', {}) != query_hash('keyword', 'oceans', {})

    def test_query_hash_binds_the_filters_not_only_the_term(self):
        # A token minted under "most liked, last month" must not be replayable
        # against the same term with the filters dropped or changed.
        liked = SearchFilters(sort_type=SortType.MOST_LIKED,
                              publish_time=PublishTime.LAST_MONTH).to_query_params()
        week = SearchFilters(sort_type=SortType.MOST_LIKED,
                             publish_time=PublishTime.LAST_WEEK).to_query_params()
        assert query_hash('keyword', 'ocean', liked) != query_hash('keyword', 'ocean', {})
        assert query_hash('keyword', 'ocean', liked) != query_hash('keyword', 'ocean', week)

    def test_query_hash_ignores_filter_param_ordering(self):
        params = SearchFilters(sort_type=SortType.MOST_LIKED,
                               publish_time=PublishTime.LAST_MONTH).to_query_params()
        assert (query_hash('keyword', 'ocean', params)
                == query_hash('keyword', 'ocean', dict(reversed(list(params.items())))))


class TestDevicePin:
    def test_missing_device_handle_is_malformed(self):
        assert _reason(_sign(_payload(dev=''))) == MALFORMED

    def test_non_string_device_handle_is_malformed(self):
        assert _reason(_sign(_payload(dev=123))) == MALFORMED

    def test_wrong_length_device_handle_is_malformed(self):
        assert _reason(_sign(_payload(dev='ab'))) == MALFORMED

    def test_handle_is_stable_per_identity_and_distinct_across_identities(self):
        assert device_handle('DEVA') == device_handle('DEVA')
        assert device_handle('DEVA') != device_handle('DEVB')

    def test_handle_does_not_carry_the_identity_value(self):
        # It rides in a token handed to an HTTP client: it must not be the
        # device id, nor a bare digest a caller could brute-force back to one.
        handle = device_handle('7300000000000000001')
        assert '7300000000000000001' not in handle
        assert handle != hashlib.sha256(b'7300000000000000001').hexdigest()[:16]


class TestEndpointValidation:
    def test_endpoints_not_a_list_is_malformed(self):
        assert _reason(_sign(_payload(eps={'p': SEARCH_VIDEO_PATH}))) == MALFORMED

    def test_no_endpoints_is_malformed(self):
        assert _reason(_sign(_payload(eps=[]))) == MALFORMED

    def test_too_many_endpoints_is_malformed(self):
        eps = [_endpoint() for _ in range(MAX_ENDPOINTS + 1)]
        assert _reason(_sign(_payload(eps=eps))) == MALFORMED

    def test_a_non_dict_endpoint_is_malformed(self):
        assert _reason(_sign(_payload(eps=[SEARCH_VIDEO_PATH]))) == MALFORMED

    def test_an_unknown_endpoint_path_is_malformed(self):
        # The path is interpolated into a signed TikTok URL: whitelist, not a
        # type check.
        assert _reason(_sign(_payload(eps=[_endpoint(p='/aweme/v1/anything/')]))) == MALFORMED

    def test_a_path_outside_the_caller_s_allowed_set_is_malformed(self):
        raw = encode(_token(EndpointState(SEARCH_USER_PATH, 10, 'S')))
        assert _reason(raw, paths=(SEARCH_VIDEO_PATH,)) == MALFORMED

    def test_non_int_cursor_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(c='10')]))) == MALFORMED

    def test_bool_cursor_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(c=True)]))) == MALFORMED

    def test_negative_cursor_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(c=-1)]))) == MALFORMED

    def test_out_of_bounds_cursor_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(c=MAX_ENDPOINT_CURSOR + 1)]))) == MALFORMED

    def test_the_cursor_bound_itself_is_accepted(self):
        token = _decode(_sign(_payload(eps=[_endpoint(c=MAX_ENDPOINT_CURSOR)])))
        assert token.endpoints[0].cursor == MAX_ENDPOINT_CURSOR

    def test_oversized_search_id_is_malformed(self):
        sid = 'a' * (MAX_SEARCH_ID_CHARS + 1)
        assert _reason(_sign(_payload(eps=[_endpoint(sid=sid)]))) == MALFORMED

    def test_bad_charset_search_id_is_malformed(self):
        # It is echoed into a signed query string, so the charset is bounded.
        assert _reason(_sign(_payload(eps=[_endpoint(sid='SID&keyword=x')]))) == MALFORMED

    def test_non_string_search_id_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(sid=None)]))) == MALFORMED

    def test_an_empty_search_id_is_allowed(self):
        # A not-yet-opened endpoint legitimately has no session.
        assert _decode(_sign(_payload(eps=[_endpoint(sid='')]))).endpoints[0].search_id == ''

    def test_non_bool_has_more_is_malformed(self):
        assert _reason(_sign(_payload(eps=[_endpoint(m=1)]))) == MALFORMED

    def test_missing_started_flag_is_malformed(self):
        endpoint = _endpoint()
        del endpoint['s']
        assert _reason(_sign(_payload(eps=[endpoint]))) == MALFORMED


class TestSeenWindowWireForm:
    def test_non_string_window_is_malformed(self):
        assert _reason(_sign(_payload(seen=[1, 2]))) == MALFORMED

    def test_a_window_that_is_not_a_whole_number_of_fingerprints_is_malformed(self):
        assert _reason(_sign(_payload(seen=paging._b64encode(b'abc')))) == MALFORMED

    def test_an_oversized_window_is_malformed(self):
        blob = b'\xff' * (paging.FINGERPRINT_BYTES * (MAX_SEEN_FINGERPRINTS + 1))
        assert _reason(_sign(_payload(seen=paging._b64encode(blob)))) == MALFORMED

    def test_a_missing_window_decodes_as_empty(self):
        payload = _payload()
        del payload['seen']
        assert _decode(_sign(payload)).seen == ()


class TestTokenSizeBound:
    def test_the_bound_is_derived_from_the_worst_case_wire_form(self):
        # Not a chosen constant: widening any field bound must move it.
        assert MAX_PAGE_TOKEN_CHARS == len(paging._wire(paging._worst_case()))

    def test_the_schema_max_length_is_the_paging_bound(self):
        # Read out of the Pydantic field METADATA. Asserting this by POSTing an
        # over-long string proves nothing: such a string is 422'd by the HMAC
        # gate anyway, so that form passes even with the bound missing.
        from tiktoksearch.api.schemas import SearchRequest

        bounds = [m.max_length for m in SearchRequest.model_fields['page_token'].metadata
                  if hasattr(m, 'max_length')]
        assert bounds == [MAX_PAGE_TOKEN_CHARS]

    def test_encode_never_exceeds_the_bound_at_the_worst_case(self):
        assert len(encode(paging._worst_case())) <= MAX_PAGE_TOKEN_CHARS

    def test_encode_sheds_the_oldest_fingerprints_to_stay_in_bounds(self):
        seen = tuple(fingerprint(str(i)) for i in range(MAX_SEEN_FINGERPRINTS + 25))
        raw = encode(_token(seen=seen))
        assert len(raw) <= MAX_PAGE_TOKEN_CHARS
        # Newest kept — they are the ones an endpoint can still re-emit.
        assert _decode(raw).seen == seen[-MAX_SEEN_FINGERPRINTS:]


class TestSeenWindow:
    def test_insertion_order_is_preserved(self):
        window = SeenWindow()
        for key in ('a', 'b', 'c'):
            window.add(key)
        assert window.recent() == (fingerprint('a'), fingerprint('b'), fingerprint('c'))

    def test_a_duplicate_add_returns_false_and_does_not_grow_the_window(self):
        window = SeenWindow()
        assert window.add('a') is True
        assert window.add('a') is False
        assert window.recent() == (fingerprint('a'),)

    def test_recent_keeps_only_the_newest_max_fingerprints(self):
        window = SeenWindow()
        for i in range(MAX_SEEN_FINGERPRINTS + 10):
            window.add(str(i))
        recent = window.recent()
        assert len(recent) == MAX_SEEN_FINGERPRINTS
        assert recent[-1] == fingerprint(str(MAX_SEEN_FINGERPRINTS + 9))
        assert fingerprint('0') not in recent

    def test_a_seeded_window_already_knows_its_keys(self):
        # This is what makes cross-request dedup work: the next request seeds
        # `seen` from the token instead of starting empty.
        window = SeenWindow((fingerprint('7'),))
        assert window.add('7') is False
        assert window.add('8') is True

    def test_fingerprints_are_fixed_width_and_stable(self):
        assert len(fingerprint('x')) == paging.FINGERPRINT_BYTES
        assert fingerprint('x') == fingerprint('x')
        assert fingerprint('x') != fingerprint('y')


class TestSanitizeSearchId:
    def test_a_usable_impr_id_is_returned_unchanged(self):
        assert sanitize_search_id('2026090412-ABC_def') == '2026090412-ABC_def'

    def test_a_missing_or_non_string_value_becomes_empty(self):
        for value in (None, '', 123, {'impr_id': 'x'}, b'bytes'):
            assert sanitize_search_id(value) == ''

    def test_an_oversized_id_is_dropped_not_truncated(self):
        # A truncated session id is a WRONG session id; '' makes the caller keep
        # the session it already has (`clean or search_id`).
        assert sanitize_search_id('a' * (MAX_SEARCH_ID_CHARS + 1)) == ''

    def test_a_bad_charset_id_is_dropped_not_stripped(self):
        assert sanitize_search_id('SID&keyword=x') == ''
        assert sanitize_search_id('SID x') == ''

    def test_whatever_it_returns_is_accepted_by_decode(self):
        # The reason it exists: mint-time bounds are decode's own bounds, so the
        # server can never issue a token its next request 422s.
        clean = sanitize_search_id('a' * MAX_SEARCH_ID_CHARS)
        assert clean
        raw = encode(_token(EndpointState(SEARCH_VIDEO_PATH, 10, clean)))
        assert _decode(raw).endpoints[0].search_id == clean
