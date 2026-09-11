"""Unit tests for `broker/envelope.py` — the outbound contract.

This file pins the message shape the requester reviewed against two live
messages and approved (`implementation_plan.md` § Contracts → Outbound
message). Five rules in it are load-bearing and silent when broken, which is
why each one is tested against a fixture built to make the break VISIBLE:

1. **`post_url` comes from `author_unique_id`, never `author_username`.** The
   display field falls back to the user-settable, non-unique `nickname`, so a
   URL built from it points at the wrong account or at nothing. A substring
   assertion cannot catch the swap — `author_username` is legitimately present
   on the record too — so the pin is an EXACT URL equality on a record whose
   two author fields deliberately differ, plus the real `flatten_video`
   impersonator record where the identity field is null and the display field
   is not.
2. **A null `author_unique_id` yields a null `post_url`.** Never fabricated
   from another field: an unverified URL that looks real is worse than a null.
3. **`source_term` MOVES.** It is absent from the top level and present in
   `metadata`, because it records which search surfaced the record — a fact
   about our scrape, not about the post.
4. **`device` and `elapsed_s` are stripped from the embedded profile.** Our
   diagnostics, not the requester's data.
5. **A reserved-key collision raises `BrokerConfigError` and NOT a
   `ValueError`.** The consumer acks `(MalformedMessage, ValidationError)`, so
   a collision spelled as a `ValueError` would ACK AWAY a perfectly good job
   because of a bug in our own `mapping.py`.

Records are built through `mapping.flatten_video` wherever the case allows, so
the top-level key set under test is the real one and not a hand-written
imitation. The two cases that need a shape `flatten_video` cannot produce —
two DIFFERENT non-null author fields — say so at the fixture.

Pure functions: no broker, no HTTP, no identities file.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import DEFAULT_CREATE_TIME, author_node  # noqa: E402

from tiktoksearch.broker.envelope import (  # noqa: E402
    PROFILE_INTERNAL_KEYS,
    RESERVED_TOP_LEVEL_KEYS,
    SCRAPED_AT_FORMAT,
    TIKTOK_BASE_URL,
    body,
    keyword_envelopes,
    page_envelopes,
    post_url,
    profile_url,
    public_profile,
    to_json,
)
from tiktoksearch.broker.errors import BrokerConfigError, MalformedMessage  # noqa: E402
from tiktoksearch.broker.messages import KeywordMessage, PageMessage, SearchType  # noqa: E402
from tiktoksearch.mapping import flatten_video  # noqa: E402

# The two live inbound samples, verbatim from the plan's § Measured broker
# facts, reduced to what the envelope layer echoes.
KEYWORD_JOB = KeywordMessage(keyword_id=5038, keyword_name='Şəki', max_results=30)
PAGE_JOB = PageMessage(page_id=1, page_name='sirabasc', page_url='https://www.tiktok.com/@sirabasc', max_posts=50)

# The moment in the plan's approved outbound sample, and its exact rendering.
MOMENT = datetime(2026, 9, 10, 8, 40, 39, tzinfo=timezone.utc)
SCRAPED_AT = '2026-09-10T08:40:39Z'

HANDLE = 'sirabasc'
POST_ID = '7680105451131882770'
SOURCE_TERM = 'search:sirabasc'

# The seven `metadata` keys the contract promises, and nothing else.
METADATA_KEYS = ('keyword_id', 'keyword_name', 'page_id', 'page_name', 'scraped_at', 'source_term', 'profile')
# The `/profile` response keys as `flatten_profile` emits them, plus the two
# fields `api/app.py` adds and this layer strips.
PROFILE_PAYLOAD = {
    'username': HANDLE, 'user_id': '7195575867517944837', 'sec_uid': 'MS4wLjABAAAAFAKE',
    'display_name': 'Sirab', 'signature': None, 'follower_count': 309729,
    'following_count': 0, 'aweme_count': 372, 'heart_count': 1001433,
    'region_code': None, 'verified': False, 'private': False,
    'avatar_url': 'https://p19-common-sign.tiktokcdn.example/fake.jpeg?x-expires=1',
    'source': 'user_search', 'device': 'FAKE-DEV-1', 'elapsed_s': 2.71,
}


def record(aweme_id=POST_ID, *, unique_id=HANDLE, nickname=None, source_term=SOURCE_TERM, **over) -> dict:
    """A REAL `mapping.flatten_video` record.

    Built through the flattener rather than written out, so the top-level key
    set these tests assert on is the one `/search` actually returns — the very
    thing the reserved-key collision check guards. `author_node(nickname=...,
    unique_id=None)` is conftest's impersonator shape: the display field
    `author_username` is filled from `unique_id or nickname` while the identity
    field `author_unique_id` reads `unique_id` alone."""
    aweme = {'aweme_id': str(aweme_id), 'author': author_node(unique_id=unique_id, nickname=nickname), 'statistics': {'play_count': 138432, 'digg_count': 916}, 'create_time': DEFAULT_CREATE_TIME}
    flat = flatten_video(aweme, source_term)
    assert flat is not None, 'the fixture itself must flatten'
    flat.update(over)
    return flat


def divergent_author_record() -> dict:
    """A record whose two author fields are BOTH non-null and DIFFERENT.

    `flatten_video` cannot produce this — it fills `author_username` from
    `unique_id or nickname`, so the two agree whenever the identity field is
    non-null — and that is exactly why the swap of one field for the other is
    invisible to every fixture built through the flattener with a handle
    present. Hand-built here, and only here, so an exact-URL assertion can
    discriminate the identity field from the display field directly."""
    return {**record(), 'author_username': 'Sirab Nickname', 'author_unique_id': HANDLE}


def keyword_envelope(post=None, **over):
    return keyword_envelopes(KEYWORD_JOB, [record(**over) if post is None else post], scraped_at=MOMENT)[0]


def page_envelope(post=None, *, profile=None, **over):
    posts = [record(**over) if post is None else post]
    return page_envelopes(PAGE_JOB, posts, PROFILE_PAYLOAD if profile is None else profile, scraped_at=MOMENT)[0]


# ============================================================== 1: post_url
class TestPostUrlComesFromTheIdentityField:
    """The rule whose breakage is silent and expensive: a URL built from the
    DISPLAY field points at the wrong account or at nothing."""

    def test_the_url_is_built_from_author_unique_id_when_the_two_fields_differ(self):
        # EXACT equality, not `in`: `author_username` is legitimately present
        # on every record, so a containment assertion passes for either field.
        post = divergent_author_record()
        assert post_url(post) == f'{TIKTOK_BASE_URL}/@{HANDLE}/video/{POST_ID}'

    def test_that_same_record_really_does_carry_a_different_username(self):
        # Both halves of the pair pinned on ONE record: otherwise the test
        # above passes again the day someone "harmonises" the two fields.
        post = divergent_author_record()
        assert post['author_username'] == 'Sirab Nickname'
        assert post['author_unique_id'] == HANDLE
        assert post['author_username'] != post['author_unique_id']
        assert 'Sirab' not in post_url(post) and 'Nickname' not in post_url(post)

    def test_a_nickname_only_author_yields_no_url_at_all(self):
        # The impersonator shape, straight out of `flatten_video`: no
        # `unique_id` upstream, so the display field carries the nickname and
        # the identity field is null. A URL here would be fabricated from a
        # user-settable, non-unique string.
        post = record(unique_id=None, nickname='Sirab Nickname')
        assert post['author_username'] == 'Sirab Nickname', 'the display field IS populated'
        assert post['author_unique_id'] is None
        assert post_url(post) is None

    @pytest.mark.parametrize('handle', [None, '', '   '])
    def test_a_missing_handle_yields_none_and_never_a_placeholder(self, handle):
        assert post_url({**record(), 'author_unique_id': handle}) is None

    @pytest.mark.parametrize('post_id', [None, '', '   '])
    def test_a_missing_id_yields_none(self, post_id):
        # Cannot arise (`flatten_video` drops a record without an `aweme_id`),
        # and covered for the same reason as the handle: the alternative to a
        # null here is a WRONG url.
        assert post_url({**record(), 'id': post_id}) is None

    def test_nothing_else_on_the_record_can_stand_in_for_the_handle(self):
        # `author_id`, `author_sec_uid` and `author_username` are all present
        # and all non-null; none of them may become the URL's handle.
        post = record(unique_id=None, nickname='Sirab Nickname')
        assert post['author_id'] and post['author_sec_uid']
        assert post_url(post) is None

    def test_the_base_url_is_the_named_constant(self):
        assert TIKTOK_BASE_URL == 'https://www.tiktok.com'
        assert post_url(record()).startswith(f'{TIKTOK_BASE_URL}/@')


class TestUrlPartsArePercentEncoded:
    """These values come from upstream, not from our own validated boundary,
    so they are encoded rather than trusted."""

    def test_an_ordinary_handle_passes_through_unchanged(self):
        assert post_url({**record(), 'author_unique_id': 'user.name_1'}) == f'{TIKTOK_BASE_URL}/@user.name_1/video/{POST_ID}'

    @pytest.mark.parametrize('handle', ['a/b', 'a?b', 'a b', 'a#b', 'a&b=c', '../../etc'])
    def test_a_handle_carrying_url_syntax_cannot_re_point_the_url(self, handle):
        url = post_url({**record(), 'author_unique_id': handle})
        assert url.startswith(f'{TIKTOK_BASE_URL}/@')
        assert url.count('/') == 5, url
        assert url.endswith(f'/video/{POST_ID}')

    def test_a_non_ascii_handle_round_trips_through_unquote(self):
        url = post_url({**record(), 'author_unique_id': 'şəki'})
        assert '%' in url
        assert unquote(url) == f'{TIKTOK_BASE_URL}/@şəki/video/{POST_ID}'

    def test_an_id_carrying_url_syntax_cannot_extend_the_path(self):
        url = post_url({**record(), 'id': '123/../../@someoneelse'})
        assert url.endswith('123%2F..%2F..%2F%40someoneelse')


# =========================================================== 2: profile_url
class TestProfileUrlComesFromTheProfilesOwnUsername:
    """What TikTok answered, not what the producer believes the account is
    called."""

    def test_it_is_built_from_username_and_not_from_the_inbound_job(self):
        # The three candidate sources deliberately DISAGREE here: the profile
        # says `realhandle`, the job's `page_name` says `producer_typo` and its
        # `page_url` says `fromtheurl`. Only the first may appear.
        job = PageMessage(page_id=1, page_name='producer_typo', page_url='https://www.tiktok.com/@fromtheurl', max_posts=5)
        envelope = page_envelopes(job, [record()], {**PROFILE_PAYLOAD, 'username': 'realhandle'}, scraped_at=MOMENT)[0]
        assert envelope.metadata.profile['profile_url'] == f'{TIKTOK_BASE_URL}/@realhandle'
        assert envelope.metadata.page_name == 'producer_typo', 'the job id is still echoed verbatim'

    def test_a_null_username_yields_a_null_url(self):
        assert profile_url(None) is None
        assert public_profile({**PROFILE_PAYLOAD, 'username': None})['profile_url'] is None

    @pytest.mark.parametrize('username', ['', '   '])
    def test_a_blank_username_yields_a_null_url(self, username):
        assert profile_url(username) is None

    def test_it_carries_no_video_path(self):
        assert profile_url(HANDLE) == f'{TIKTOK_BASE_URL}/@{HANDLE}'

    def test_a_username_carrying_url_syntax_is_encoded(self):
        assert profile_url('a/b') == f'{TIKTOK_BASE_URL}/@a%2Fb'


# ======================================================== 3: public_profile
class TestTheEmbeddedProfileIsStripped:
    """`device` and `elapsed_s` are OUR diagnostics — which pooled device
    served the call and how long it took — and are not the requester's data."""

    @pytest.mark.parametrize('key', PROFILE_INTERNAL_KEYS)
    def test_an_internal_diagnostic_is_removed(self, key):
        assert key in PROFILE_PAYLOAD, 'the fixture really does carry it'
        assert key not in public_profile(PROFILE_PAYLOAD)

    def test_the_strip_list_is_exactly_the_two_diagnostics(self):
        assert PROFILE_INTERNAL_KEYS == ('device', 'elapsed_s')

    def test_the_device_value_appears_nowhere_in_the_published_body(self):
        # Asserting the KEY's absence is not enough: the point is that the
        # device id does not reach the requester at all.
        published = to_json(page_envelope()).decode('utf-8')
        assert PROFILE_PAYLOAD['device'] not in published
        assert 'elapsed_s' not in published

    def test_every_other_profile_key_survives_verbatim(self):
        public = public_profile(PROFILE_PAYLOAD)
        for key, value in PROFILE_PAYLOAD.items():
            if key not in PROFILE_INTERNAL_KEYS:
                assert public[key] == value, key

    def test_profile_url_is_the_only_key_added(self):
        added = set(public_profile(PROFILE_PAYLOAD)) - set(PROFILE_PAYLOAD)
        assert added == {'profile_url'}

    def test_the_interim_nulls_are_republished_as_nulls(self):
        # `signature` and `region_code` are STRUCTURALLY null on the
        # `user_search` source — not "this account has no bio" — so they must
        # be present and null rather than dropped.
        public = public_profile(PROFILE_PAYLOAD)
        assert public['signature'] is None
        assert public['region_code'] is None

    def test_the_input_mapping_is_not_mutated(self):
        before = dict(PROFILE_PAYLOAD)
        public_profile(PROFILE_PAYLOAD)
        assert PROFILE_PAYLOAD == before


# =========================================== 4: one message per post, fanned
class TestOneEnvelopePerPost:
    """"One message per post" — the fan-out itself."""

    def test_twelve_posts_yield_twelve_envelopes(self):
        posts = [record(7_680_105_451_131_880_000 + n) for n in range(12)]
        assert len(keyword_envelopes(KEYWORD_JOB, posts, scraped_at=MOMENT)) == 12

    def test_two_hundred_posts_yield_two_hundred_envelopes_with_distinct_urls(self):
        posts = [record(7_680_105_451_131_880_000 + n) for n in range(200)]
        envelopes = page_envelopes(PAGE_JOB, posts, PROFILE_PAYLOAD, scraped_at=MOMENT)
        assert len(envelopes) == 200
        urls = [envelope.post_url for envelope in envelopes]
        assert len(set(urls)) == 200
        assert all(url is not None for url in urls)

    def test_the_order_of_the_result_list_is_preserved(self):
        # The API already ordered these (newest-first on `/user/posts`), so
        # re-ordering or shuffling here would silently undo that.
        posts = [record(n) for n in (300, 100, 200)]
        envelopes = page_envelopes(PAGE_JOB, posts, PROFILE_PAYLOAD, scraped_at=MOMENT)
        assert [envelope.record['id'] for envelope in envelopes] == ['300', '100', '200']

    @pytest.mark.parametrize('builder', ['keyword', 'page'])
    def test_an_empty_result_list_yields_zero_envelopes(self, builder):
        # Zero posts is a SUCCESS, not an error: nothing is published for a job
        # that surfaced nothing.
        if builder == 'keyword':
            assert keyword_envelopes(KEYWORD_JOB, [], scraped_at=MOMENT) == []
        else:
            assert page_envelopes(PAGE_JOB, [], PROFILE_PAYLOAD, scraped_at=MOMENT) == []

    def test_one_stamp_is_shared_by_every_envelope_of_a_job(self):
        posts = [record(n) for n in range(5)]
        envelopes = keyword_envelopes(KEYWORD_JOB, posts)
        assert len({envelope.metadata.scraped_at for envelope in envelopes}) == 1

    def test_the_source_record_is_not_mutated(self):
        post = record()
        before = dict(post)
        keyword_envelopes(KEYWORD_JOB, [post], scraped_at=MOMENT)
        assert post == before, 'the `source_term` pop must not reach the caller\'s dict'


# ================================= 5: which queue produced which envelope
class TestSearchTypeAndTheProfileFollowTheQueue:
    """`search_type` is top level because it describes the post as far as a
    consumer is concerned; `profile` is in `metadata` because it does not."""

    def test_a_keyword_job_is_marked_keyword(self):
        assert keyword_envelope().search_type is SearchType.KEYWORD

    def test_a_page_job_is_marked_page(self):
        assert page_envelope().search_type is SearchType.PAGE

    def test_a_keyword_result_carries_no_profile(self):
        assert keyword_envelope().metadata.profile is None

    def test_a_page_result_carries_the_profile(self):
        profile = page_envelope().metadata.profile
        assert isinstance(profile, dict)
        assert profile['username'] == HANDLE
        assert profile['follower_count'] == 309729

    def test_the_profile_is_repeated_on_every_message_of_a_page_job(self):
        # Deliberately not sent once: the contract is one SELF-DESCRIBING
        # message per post, so a consumer reading a single message must not
        # have to correlate it with another.
        envelopes = page_envelopes(PAGE_JOB, [record(1), record(2)], PROFILE_PAYLOAD, scraped_at=MOMENT)
        assert [envelope.metadata.profile['username'] for envelope in envelopes] == [HANDLE, HANDLE]

    def test_a_keyword_result_echoes_the_keyword_ids_and_nulls_the_page_ids(self):
        metadata = keyword_envelope().metadata
        assert (metadata.keyword_id, metadata.keyword_name) == (5038, 'Şəki')
        assert (metadata.page_id, metadata.page_name) == (None, None)

    def test_a_page_result_echoes_the_page_ids_and_nulls_the_keyword_ids(self):
        metadata = page_envelope().metadata
        assert (metadata.page_id, metadata.page_name) == (1, 'sirabasc')
        assert (metadata.keyword_id, metadata.keyword_name) == (None, None)


# ================================================== 6: source_term MOVES
class TestSourceTermMovesIntoMetadata:
    """It records which search surfaced the record — a fact about our scrape,
    not about the post — so it is the ONE record field that moves rather than
    passing through."""

    def test_it_is_absent_from_the_top_level(self):
        assert 'source_term' in record(), 'flatten_video really does emit it'
        assert 'source_term' not in body(keyword_envelope())

    def test_it_is_present_in_metadata_with_the_records_own_value(self):
        assert body(keyword_envelope())['metadata']['source_term'] == SOURCE_TERM

    def test_it_moves_on_the_page_queue_too(self):
        published = body(page_envelope())
        assert 'source_term' not in published
        assert published['metadata']['source_term'] == SOURCE_TERM

    def test_a_record_without_one_yields_a_null_rather_than_a_missing_key(self):
        post = record()
        del post['source_term']
        published = body(keyword_envelopes(KEYWORD_JOB, [post], scraped_at=MOMENT)[0])
        assert published['metadata']['source_term'] is None
        assert 'source_term' in published['metadata']

    def test_no_other_record_field_moves_or_is_dropped(self):
        # A consumer that already reads `/search` results must read these
        # bodies with the same code, MINUS `source_term` and plus the two
        # fields this layer owns.
        post = record()
        published = body(keyword_envelope(post=post))
        expected = {key: value for key, value in post.items() if key != 'source_term'}
        for key, value in expected.items():
            assert published[key] == value, key
        assert set(published) == set(expected) | set(RESERVED_TOP_LEVEL_KEYS)


# ================================================= 7: the body's two levels
class TestTheMessageIsThePost:
    """No wrapper object. The record's own fields ARE the body's top level."""

    def test_there_is_no_post_wrapper(self):
        published = body(keyword_envelope())
        assert 'post' not in published
        assert 'record' not in published
        assert published['id'] == POST_ID
        assert 'description' in published and 'hashtags' in published

    def test_the_top_level_is_the_record_plus_exactly_three_keys(self):
        post = record()
        published = body(keyword_envelope(post=post))
        assert set(published) - set(post) == set(RESERVED_TOP_LEVEL_KEYS)
        assert set(post) - set(published) == {'source_term'}

    def test_metadata_holds_the_seven_contract_keys_and_nothing_else(self):
        assert tuple(body(page_envelope())['metadata']) == METADATA_KEYS

    def test_search_type_serialises_as_a_plain_string(self):
        # An enum member would render as `SearchType.PAGE` through `repr` and
        # break every consumer's equality test.
        published = body(page_envelope())
        assert published['search_type'] == 'page'
        assert isinstance(published['search_type'], str) and not isinstance(published['search_type'], SearchType)

    def test_a_null_post_url_is_present_as_null_rather_than_dropped(self):
        # "Every key ALWAYS present" — a consumer never needs a key-existence
        # check, so a null must serialise as `null` and not vanish.
        published = body(keyword_envelope(unique_id=None, nickname='Sirab Nickname'))
        assert 'post_url' in published
        assert published['post_url'] is None

    def test_every_metadata_null_is_present_as_null(self):
        published = body(keyword_envelope())
        assert tuple(published['metadata']) == METADATA_KEYS
        assert published['metadata']['page_id'] is None
        assert published['metadata']['profile'] is None

    def test_the_reserved_key_tuple_is_the_three_this_layer_owns(self):
        assert RESERVED_TOP_LEVEL_KEYS == ('post_url', 'search_type', 'metadata')


class TestTheJsonOnTheWire:
    def test_it_is_utf8_json_that_round_trips(self):
        raw = to_json(page_envelope())
        assert isinstance(raw, bytes)
        assert json.loads(raw.decode('utf-8')) == body(page_envelope())

    def test_non_ascii_text_is_not_escaped(self):
        # The body is declared UTF-8, so an Azerbaijani keyword stays readable
        # on the wire instead of becoming `\\uXXXX` escapes.
        raw = to_json(keyword_envelope())
        assert 'Şəki'.encode('utf-8') in raw
        assert b'\\u' not in raw

    def test_the_published_body_matches_the_approved_sample_shape(self):
        # The whole contract, once, end to end: the plan's own sample values.
        published = json.loads(to_json(page_envelope()).decode('utf-8'))
        assert published['id'] == POST_ID
        assert published['author_unique_id'] == HANDLE
        assert published['post_url'] == f'{TIKTOK_BASE_URL}/@{HANDLE}/video/{POST_ID}'
        assert published['search_type'] == 'page'
        assert published['metadata']['page_id'] == 1
        assert published['metadata']['page_name'] == HANDLE
        assert published['metadata']['scraped_at'] == SCRAPED_AT
        assert published['metadata']['source_term'] == SOURCE_TERM
        assert published['metadata']['profile']['profile_url'] == f'{TIKTOK_BASE_URL}/@{HANDLE}'


# =============================================== 8: the two time spellings
class TestScrapedAt:
    """The worker's UTC publish time, `Z`-suffixed — deliberately spelled
    differently from the record's own `create_time` (`+00:00`, from
    `mapping._iso_utc`, whose fixed 25-character width the posts ordering leans
    on). Both are UTC; the divergence is known and was accepted."""

    def test_it_renders_the_plans_own_sample_value(self):
        assert keyword_envelope().metadata.scraped_at == SCRAPED_AT

    def test_it_ends_with_z_and_not_with_an_offset(self):
        stamp = keyword_envelope().metadata.scraped_at
        assert stamp.endswith('Z')
        assert '+00:00' not in stamp
        assert len(stamp) == 20

    def test_the_records_own_create_time_keeps_the_offset_spelling(self):
        # The accepted divergence, pinned on ONE message so "harmonising" the
        # two fails here rather than silently changing the record rendering the
        # posts ordering depends on.
        published = body(keyword_envelope())
        assert published['create_time'].endswith('+00:00')
        assert len(published['create_time']) == 25
        assert published['metadata']['scraped_at'].endswith('Z')

    def test_the_format_constant_is_the_contract_spelling(self):
        assert SCRAPED_AT_FORMAT == '%Y-%m-%dT%H:%M:%SZ'

    def test_a_non_utc_instant_is_converted_rather_than_relabelled(self):
        # Baku is UTC+4, so the same instant renders as the same UTC stamp. A
        # `strftime` without the `astimezone` would emit 12:40:39Z — the wrong
        # instant, labelled UTC.
        baku = datetime(2026, 9, 10, 12, 40, 39, tzinfo=timezone(timedelta(hours=4)))
        assert keyword_envelopes(KEYWORD_JOB, [record()], scraped_at=baku)[0].metadata.scraped_at == SCRAPED_AT

    def test_a_naive_datetime_is_refused(self):
        # It would be read as LOCAL time by `astimezone` and silently stamped
        # with the wrong instant.
        with pytest.raises(ValueError):
            keyword_envelopes(KEYWORD_JOB, [record()], scraped_at=datetime(2026, 9, 10, 8, 40, 39))

    def test_an_omitted_stamp_is_filled_in(self):
        stamp = keyword_envelopes(KEYWORD_JOB, [record()])[0].metadata.scraped_at
        assert stamp.endswith('Z') and len(stamp) == 20


# ============================================= 9: the collision escape hatch
class TestAReservedKeyCollisionRaises:
    """The record shares the body's top-level namespace with the three fields
    this layer owns, so a future `mapping.py` field named one of them would
    either overwrite ours or be overwritten by it — with no symptom but a wrong
    value in every message.

    The CLASS of the error is the load-bearing part. The consumer's ack policy
    catches `(MalformedMessage, ValidationError)` and ACKS; a collision spelled
    as either would drop a perfectly good job because of a bug in our own
    `mapping.py`. `BrokerConfigError` escapes the policy, so the worker stops
    with the inbound message still unacked and the broker requeues the job."""

    @pytest.mark.parametrize('key', RESERVED_TOP_LEVEL_KEYS)
    def test_a_colliding_record_field_is_detected_on_the_keyword_queue(self, key):
        with pytest.raises(BrokerConfigError):
            keyword_envelopes(KEYWORD_JOB, [{**record(), key: 'whatever the mapping now emits'}], scraped_at=MOMENT)

    @pytest.mark.parametrize('key', RESERVED_TOP_LEVEL_KEYS)
    def test_a_colliding_record_field_is_detected_on_the_page_queue_too(self, key):
        with pytest.raises(BrokerConfigError):
            page_envelopes(PAGE_JOB, [{**record(), key: 'whatever'}], PROFILE_PAYLOAD, scraped_at=MOMENT)

    @pytest.mark.parametrize('key', RESERVED_TOP_LEVEL_KEYS)
    def test_a_colliding_field_is_never_silently_overwritten(self, key):
        # The alternative implementation — "log a warning and let ours win" —
        # returns an envelope instead of raising. This is the assertion that
        # tells the two apart.
        with pytest.raises(BrokerConfigError):
            keyword_envelopes(KEYWORD_JOB, [{**record(), key: 'SENTINEL'}], scraped_at=MOMENT)

    def test_it_is_not_a_value_error_so_the_ack_policy_cannot_ack_it(self):
        assert not issubclass(BrokerConfigError, ValueError)
        assert not issubclass(BrokerConfigError, MalformedMessage)
        assert issubclass(BrokerConfigError, RuntimeError)

    def test_the_policys_own_except_clause_does_not_catch_it(self):
        # The policy verbatim: `except (MalformedMessage, ValidationError)`.
        # Written out here because the discrimination is between two exception
        # CLASSES and a subclass check alone would not show that the real
        # clause lets this one through.
        from pydantic import ValidationError
        with pytest.raises(BrokerConfigError):
            try:
                keyword_envelopes(KEYWORD_JOB, [{**record(), 'post_url': 'x'}], scraped_at=MOMENT)
            except (MalformedMessage, ValidationError) as exc:  # pragma: no cover - the bug this pins
                raise AssertionError(f'the ack policy would have ACKED a good job: {type(exc).__name__}')

    def test_the_message_names_the_key_and_carries_no_record_value(self):
        with pytest.raises(BrokerConfigError) as caught:
            keyword_envelopes(KEYWORD_JOB, [{**record(), 'metadata': 'SECRET-RECORD-VALUE'}], scraped_at=MOMENT)
        assert 'metadata' in str(caught.value)
        assert 'SECRET-RECORD-VALUE' not in str(caught.value)

    def test_every_colliding_key_is_named_when_more_than_one_collides(self):
        with pytest.raises(BrokerConfigError) as caught:
            keyword_envelopes(KEYWORD_JOB, [{**record(), 'post_url': 'a', 'search_type': 'b'}], scraped_at=MOMENT)
        assert 'post_url' in str(caught.value) and 'search_type' in str(caught.value)

    def test_todays_flattener_does_not_collide(self):
        # MEASURED, so the check above is a tripwire and not a permanently
        # firing guard: were it firing today, every test in this file that
        # builds a real record would be exercising the raise instead of the
        # envelope.
        assert set(record()) & set(RESERVED_TOP_LEVEL_KEYS) == set()
        assert len(record()) == 17, 'envelope.py records 17 flatten_video keys; the count moved'
