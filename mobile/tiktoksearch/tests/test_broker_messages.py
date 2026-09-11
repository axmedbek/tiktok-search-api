"""Unit tests for `broker/messages.py` and `broker/handle.py`.

The inbound boundary. Two things are pinned here and nothing else:

1. **The two live message shapes parse.** `LIVE_KEYWORD_BODY` and
   `LIVE_PAGE_BODY` below are the bodies READ OFF THE LIVE BROKER and quoted
   verbatim in `implementation_plan.md` § Measured broker facts — not plausible
   inventions. They are the canonical fixtures for the whole broker suite, so
   the field names and types under test are the producer's real ones. A field
   the plan lists as ignored (`is_auto_generated`, `is_combined`, `timestamp`)
   must stay ignored: declaring it would advertise that this worker acts on it.
2. **`handle_from_page`'s precedence and its refusal.** `page_url` WINS over
   `page_name`, and a `page_url` that is present but is not a TikTok profile
   URL is REJECTED rather than fallen back on — the job disagrees with itself
   about which account it is for, and scraping `page_name` anyway would answer
   a question nobody asked. That refusal is the one rule in this file whose
   breakage is silent and expensive: the fallback would look like resilience
   and would scrape the wrong account.

No I/O of any kind: pure models and a pure URL parser. `mobile/identities.json`
is never read, no credential appears in any fixture, and conftest's session
network tripwire still stands over every test.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch.broker.errors import MalformedMessage  # noqa: E402
from tiktoksearch.broker.handle import (  # noqa: E402
    _NO_USABLE_HANDLE,
    _NOT_A_PROFILE_URL,
    handle_from_page,
)
from tiktoksearch.broker.messages import (  # noqa: E402
    MAX_INBOUND_LIMIT,
    MAX_NAME_CHARS,
    MAX_PAGE_URL_CHARS,
    KeywordMessage,
    Metadata,
    PageMessage,
    SearchType,
)
from tiktoksearch.filters import PublishTime, SortType  # noqa: E402
from tiktoksearch.limits import MAX_USERNAME_CHARS  # noqa: E402

# --------------------------------------------------------------------------
# The two live samples, verbatim from `implementation_plan.md` § Measured
# broker facts. Read from the live broker's management API with
# `ackmode: reject_requeue_true` — nothing was consumed to obtain them.
# Copied rather than paraphrased: the value of a fixture read off a real
# producer is precisely that nobody chose its shape.
# --------------------------------------------------------------------------
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

# The bounds, as LITERALS beside the cases. Deliberately not spelled
# `MAX_INBOUND_LIMIT + 1`: a case that moves with the constant it is bounding
# passes at any ceiling, which is the third worthless-assertion shape on
# record in `agent_docs/testing.md`. Widening a bound must fail here first.
INBOUND_LIMIT = 300
NAME_CHARS = 200
PAGE_URL_CHARS = 2048
USERNAME_CHARS = 24

# Fields the plan lists as IGNORED for each queue.
KEYWORD_IGNORED = ('is_auto_generated', 'is_combined', 'timestamp')
PAGE_IGNORED = ('timestamp',)


def keyword_body(**over) -> dict:
    """The live keyword body with overrides; a value of `...` drops the key."""
    return {key: value for key, value in {**LIVE_KEYWORD_BODY, **over}.items() if value is not ...}


def page_body(**over) -> dict:
    """The live page body with overrides; a value of `...` drops the key."""
    return {key: value for key, value in {**LIVE_PAGE_BODY, **over}.items() if value is not ...}


# ================================================== 1: the bounds themselves
class TestTheBoundsAreWhereTheCasesThinkTheyAre:
    """Every bound below is asserted as a literal, once, here.

    So the parametrised rejection cases further down can use those literals
    instead of `CONSTANT + 1` — an expression that follows the constant and
    therefore proves nothing about where the ceiling is."""

    def test_the_inbound_limit_ceiling_is_three_hundred(self):
        assert MAX_INBOUND_LIMIT == INBOUND_LIMIT

    def test_the_name_ceiling_is_two_hundred(self):
        assert MAX_NAME_CHARS == NAME_CHARS

    def test_the_page_url_ceiling_is_two_thousand_and_forty_eight(self):
        assert MAX_PAGE_URL_CHARS == PAGE_URL_CHARS

    def test_the_handle_ceiling_is_twenty_four(self):
        assert MAX_USERNAME_CHARS == USERNAME_CHARS


# ============================================= 2: the live keyword message
class TestTheLiveKeywordMessage:
    """The measured body off `sm.scraping.tiktok.keyword`."""

    def test_the_live_sample_parses_to_its_measured_values(self):
        message = KeywordMessage.model_validate(LIVE_KEYWORD_BODY)
        assert message.keyword_id == 5038
        assert message.keyword_name == 'Şəki'
        assert message.max_results == 30

    def test_the_live_sample_carries_no_filters(self):
        # Both filters were NULL in the measured sample, and null means "no
        # filter" — never a default one, which would silently scrape a
        # different slice than was asked for.
        message = KeywordMessage.model_validate(LIVE_KEYWORD_BODY)
        assert message.sort_type is None
        assert message.publish_time is None

    @pytest.mark.parametrize('field', KEYWORD_IGNORED)
    def test_a_field_the_plan_ignores_is_not_declared(self, field):
        # Not merely "unused": ABSENT from the model. A declared field would
        # advertise to the next reader that this worker acts on it.
        message = KeywordMessage.model_validate(LIVE_KEYWORD_BODY)
        assert field in LIVE_KEYWORD_BODY, 'the live sample really does carry it'
        assert field not in message.model_dump()
        assert not hasattr(message, field)

    def test_an_unknown_extra_field_is_tolerated(self):
        # The producer is a separate system that may add a field at any time,
        # and an unparseable body is ACKED AND DROPPED — so rejecting an
        # unknown key would silently discard every job after such a change.
        message = KeywordMessage.model_validate(keyword_body(newly_added_upstream_field=['whatever']))
        assert message.keyword_id == 5038
        assert 'newly_added_upstream_field' not in message.model_dump()

    @pytest.mark.parametrize('field', ['keyword_id', 'keyword_name', 'max_results'])
    def test_a_missing_required_field_is_a_validation_error(self, field):
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(**{field: ...}))

    @pytest.mark.parametrize('name', ['  Şəki  ', '\tŞəki\n'])
    def test_surrounding_whitespace_is_stripped_from_the_name(self, name):
        assert KeywordMessage.model_validate(keyword_body(keyword_name=name)).keyword_name == 'Şəki'

    @pytest.mark.parametrize('name', ['', '   ', '\t\n'])
    def test_a_whitespace_only_name_is_rejected_rather_than_searched(self, name):
        # `min_length=1` counts spaces, so without the strip a name of two
        # spaces parsed, became `POST /search` `query="  "`, and spent a signed
        # request plus a daily-cap unit on nothing. The cap is this project's
        # scarce resource.
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(keyword_name=name))

    def test_a_name_at_the_ceiling_is_accepted(self):
        assert len(KeywordMessage.model_validate(keyword_body(keyword_name='k' * NAME_CHARS)).keyword_name) == NAME_CHARS

    def test_a_name_one_character_over_the_ceiling_is_rejected(self):
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(keyword_name='k' * (NAME_CHARS + 1)))

    @pytest.mark.parametrize('limit', [1, INBOUND_LIMIT])
    def test_a_limit_inside_the_range_is_accepted(self, limit):
        assert KeywordMessage.model_validate(keyword_body(max_results=limit)).max_results == limit

    @pytest.mark.parametrize('limit', [0, -1, INBOUND_LIMIT + 1, 10_000])
    def test_a_limit_outside_the_range_is_rejected(self, limit):
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(max_results=limit))


class TestTheUnmeasuredFilterSpelling:
    """Both filters were null in the only measured sample, so the producer's
    NON-null wire type is unmeasured: `1` and `"1"` are equally plausible.

    `SortType`/`PublishTime` are str-valued enums and pydantic does not coerce
    int -> str, so the int spelling would be read as a malformed body and the
    whole job acked away. The coercion changes a value's SPELLING, never the
    value."""

    @pytest.mark.parametrize('raw,expected', [('1', SortType.MOST_LIKED), (1, SortType.MOST_LIKED), ('0', SortType.RELEVANCE), (0, SortType.RELEVANCE)])
    def test_sort_type_accepts_both_spellings_of_the_same_value(self, raw, expected):
        assert KeywordMessage.model_validate(keyword_body(sort_type=raw)).sort_type is expected

    @pytest.mark.parametrize('raw,expected', [('30', PublishTime.LAST_MONTH), (30, PublishTime.LAST_MONTH), (180, PublishTime.LAST_6_MONTHS)])
    def test_publish_time_accepts_both_spellings_of_the_same_value(self, raw, expected):
        assert KeywordMessage.model_validate(keyword_body(publish_time=raw)).publish_time is expected

    @pytest.mark.parametrize('field', ['sort_type', 'publish_time'])
    @pytest.mark.parametrize('value', [True, False])
    def test_a_bool_is_not_a_filter_value(self, field, value):
        # `bool` is an `int` subclass, so the numeric branch would coerce
        # `True` to `'True'` and `False` to `'False'` — neither of which is a
        # filter. Handed back for pydantic to report as the type error it is.
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(**{field: value}))

    @pytest.mark.parametrize('field', ['sort_type', 'publish_time'])
    @pytest.mark.parametrize('value', [99, '99', 'most_liked', [], {}])
    def test_a_value_outside_the_enum_is_rejected_in_either_spelling(self, field, value):
        with pytest.raises(ValidationError):
            KeywordMessage.model_validate(keyword_body(**{field: value}))

    @pytest.mark.parametrize('field', ['sort_type', 'publish_time'])
    def test_an_absent_filter_is_none_and_never_a_default(self, field):
        message = KeywordMessage.model_validate(keyword_body(**{field: ...}))
        assert getattr(message, field) is None


# ================================================ 3: the live page message
class TestTheLivePageMessage:
    """The measured body off `sm.scraping.tiktok.page`."""

    def test_the_live_sample_parses_to_its_measured_values(self):
        message = PageMessage.model_validate(LIVE_PAGE_BODY)
        assert message.page_id == 1
        assert message.page_name == 'sirabasc'
        assert message.page_url == 'https://www.tiktok.com/@sirabasc'
        assert message.max_posts == 50

    @pytest.mark.parametrize('field', PAGE_IGNORED)
    def test_a_field_the_plan_ignores_is_not_declared(self, field):
        message = PageMessage.model_validate(LIVE_PAGE_BODY)
        assert field in LIVE_PAGE_BODY, 'the live sample really does carry it'
        assert field not in message.model_dump()

    def test_an_unknown_extra_field_is_tolerated(self):
        message = PageMessage.model_validate(page_body(newly_added_upstream_field=7))
        assert message.page_id == 1
        assert 'newly_added_upstream_field' not in message.model_dump()

    @pytest.mark.parametrize('field', ['page_id', 'page_name', 'max_posts'])
    def test_a_missing_required_field_is_a_validation_error(self, field):
        with pytest.raises(ValidationError):
            PageMessage.model_validate(page_body(**{field: ...}))

    def test_page_url_is_optional_because_page_name_is_the_fallback(self):
        # A message without it is still serviceable: `page_url` is the
        # PREFERRED source of the handle, not the only one.
        assert PageMessage.model_validate(page_body(page_url=...)).page_url is None

    def test_a_whitespace_only_page_name_is_rejected(self):
        with pytest.raises(ValidationError):
            PageMessage.model_validate(page_body(page_name='   '))

    def test_a_page_name_at_the_ceiling_is_accepted(self):
        assert len(PageMessage.model_validate(page_body(page_name='p' * NAME_CHARS)).page_name) == NAME_CHARS

    def test_a_page_name_one_character_over_the_ceiling_is_rejected(self):
        with pytest.raises(ValidationError):
            PageMessage.model_validate(page_body(page_name='p' * (NAME_CHARS + 1)))

    def test_a_page_url_at_the_ceiling_is_accepted(self):
        # Bounded so a runaway producer string cannot reach the URL parser at
        # all. Not validated as a URL here — `handle_from_page` owns that.
        url = 'https://www.tiktok.com/@' + 'x' * (PAGE_URL_CHARS - len('https://www.tiktok.com/@'))
        assert len(PageMessage.model_validate(page_body(page_url=url)).page_url) == PAGE_URL_CHARS

    def test_a_page_url_one_character_over_the_ceiling_is_rejected(self):
        with pytest.raises(ValidationError):
            PageMessage.model_validate(page_body(page_url='h' * (PAGE_URL_CHARS + 1)))

    @pytest.mark.parametrize('limit', [0, -1, INBOUND_LIMIT + 1])
    def test_a_max_posts_outside_the_range_is_rejected(self, limit):
        with pytest.raises(ValidationError):
            PageMessage.model_validate(page_body(max_posts=limit))


# ======================================== 4: which handle a page job is for
class TestPageUrlWinsOverPageName:
    """The precedence rule, on a message where the two DISAGREE.

    A fixture whose `page_url` handle equals its `page_name` — which the live
    sample's does — cannot tell the two sources apart, so every precedence
    case here is built on a deliberate disagreement."""

    def test_the_url_handle_is_used_when_it_disagrees_with_the_name(self):
        assert handle_from_page('https://www.tiktok.com/@fromtheurl', 'fromthename') == 'fromtheurl'

    @pytest.mark.parametrize('page_url', [None, '', '   ', '\t'])
    def test_an_absent_or_blank_url_falls_back_to_the_name(self, page_url):
        assert handle_from_page(page_url, 'fromthename') == 'fromthename'

    def test_the_live_sample_yields_its_handle(self):
        message = PageMessage.model_validate(LIVE_PAGE_BODY)
        assert handle_from_page(message.page_url, message.page_name) == 'sirabasc'


class TestABadUrlIsRefusedAndNeverFallenBackOn:
    """The expensive-if-broken rule. A `page_url` that is present but is not a
    TikTok profile URL means the job disagrees with itself about which account
    it is for.

    Falling back to `page_name` here would look like resilience and would
    scrape a DIFFERENT account than the one the job named — silently, with a
    perfectly well-formed result published under the wrong page ids. Every case
    below therefore passes a page_name that WOULD have succeeded on its own."""

    USABLE_NAME = 'perfectlygoodname'

    @pytest.mark.parametrize('page_url', [
        'https://www.instagram.com/@bob',
        'https://www.tiktok.com.evil.example/@bob',
        'https://nottiktok.com/@bob',
        'ftp://www.tiktok.com/@bob',
        'javascript:alert(1)',
        '/@bob',
        'www.tiktok.com/@bob',
        'https://www.tiktok.com/',
        'https://www.tiktok.com',
        'https://www.tiktok.com/tag/foo',
        'https://www.tiktok.com/music/x-123',
        'https://www.tiktok.com/@',
        'https://www.tiktok.com/@@bob',
        'https://www.tiktok.com/@Şeki',
        'https://www.tiktok.com/@%C5%9Feki',
        'https://www.tiktok.com/@bad-handle',
        'https://www.tiktok.com<>/@bob',
        'https://[::1/@bob',
    ])
    def test_a_url_that_is_not_a_tiktok_profile_url_raises(self, page_url):
        with pytest.raises(MalformedMessage) as caught:
            handle_from_page(page_url, self.USABLE_NAME)
        assert self.USABLE_NAME not in str(caught.value), 'the name must not have been used'
        assert str(caught.value) == _NOT_A_PROFILE_URL

    def test_the_userinfo_trick_resolves_to_the_real_host_and_is_refused(self):
        # A browser resolves `https://www.tiktok.com@evil.com/@x` to
        # `evil.com`, and so must we: the host is what follows the LAST '@' in
        # the authority.
        with pytest.raises(MalformedMessage):
            handle_from_page('https://www.tiktok.com@evil.com/@x', self.USABLE_NAME)

    # MEASURED, on this interpreter: `urlparse` raises for exactly these two —
    # "netloc '...' contains invalid characters under NFKC normalization"
    # (which echoes the producer's netloc straight back) and "Invalid IPv6
    # URL". `https://www.tiktok.com<>/@bob` parses fine and is refused one
    # step later by the host check, so it is NOT a case for this test.
    @pytest.mark.parametrize('page_url', ['https://www.tiktok.com﹫evil.example/@x', 'https://[::1/@bob'])
    def test_a_url_the_parser_itself_rejects_leaves_as_our_own_rejection(self, page_url):
        # Those `ValueError`s are not ours and their text is not ours.
        # Uncaught, they left this module as a rejection whose wording we do
        # not own, carrying producer bytes into a WARNING log line.
        with pytest.raises(MalformedMessage) as caught:
            handle_from_page(page_url, self.USABLE_NAME)
        assert str(caught.value) == _NOT_A_PROFILE_URL
        assert 'evil' not in str(caught.value)

    def test_no_rejection_text_repeats_the_offending_url(self):
        with pytest.raises(MalformedMessage) as caught:
            handle_from_page('https://evil.example/@bob?token=abc', self.USABLE_NAME)
        assert 'evil.example' not in str(caught.value)
        assert 'token' not in str(caught.value)


class TestUrlShapesThatDoYieldAHandle:
    """Everything a real profile URL is allowed to look like."""

    @pytest.mark.parametrize('page_url,expected', [
        ('https://www.tiktok.com/@sirabasc', 'sirabasc'),
        ('http://www.tiktok.com/@sirabasc', 'sirabasc'),
        ('https://tiktok.com/@sirabasc', 'sirabasc'),
        ('https://m.tiktok.com/@sirabasc', 'sirabasc'),
        ('https://WWW.TikTok.COM/@sirabasc', 'sirabasc'),
        ('https://www.tiktok.com:443/@sirabasc', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc/', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc///', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc?lang=az&is_from_webapp=1', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc/?lang=az', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc#top', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc/video/7680105451131882770', 'sirabasc'),
        ('https://www.tiktok.com/@sirabasc/live', 'sirabasc'),
        ('  https://www.tiktok.com/@sirabasc  ', 'sirabasc'),
        ('https://www.tiktok.com/%40sirabasc', 'sirabasc'),
        ('https://www.tiktok.com/@user.name_1', 'user.name_1'),
        ('https://www.tiktok.com/@' + 'x' * USERNAME_CHARS, 'x' * USERNAME_CHARS),
    ])
    def test_the_first_path_segment_is_the_handle(self, page_url, expected):
        assert handle_from_page(page_url, 'ignored') == expected

    def test_a_percent_encoded_at_sign_is_decoded_before_the_handle_test(self):
        # A browser resolves `https://www.tiktok.com/%40bob` to a real profile
        # and so must we. Undecoded it read as a segment with no '@' and the
        # job was dropped permanently and silently.
        assert handle_from_page('https://www.tiktok.com/%40bob', 'ignored') == 'bob'

    def test_decoding_widens_what_is_accepted_but_not_what_is_forwarded(self):
        # The charset and length checks run on the DECODED value, so a
        # percent-encoded non-ASCII handle is still refused.
        with pytest.raises(MalformedMessage):
            handle_from_page('https://www.tiktok.com/%40%C5%9Feki', 'ignored')

    def test_a_handle_one_character_over_the_ceiling_is_refused(self):
        with pytest.raises(MalformedMessage):
            handle_from_page('https://www.tiktok.com/@' + 'x' * (USERNAME_CHARS + 1), 'ignored')


class TestTheNameFallback:
    """`page_name` is whatever the producer typed, so it is normalised and
    charset-checked exactly as the URL path is."""

    @pytest.mark.parametrize('page_name,expected', [
        ('sirabasc', 'sirabasc'),
        ('@sirabasc', 'sirabasc'),
        ('  @sirabasc  ', 'sirabasc'),
        ('user.name_1', 'user.name_1'),
        ('x' * USERNAME_CHARS, 'x' * USERNAME_CHARS),
    ])
    def test_a_usable_name_is_normalised(self, page_name, expected):
        assert handle_from_page(None, page_name) == expected

    @pytest.mark.parametrize('page_name', [
        None, '', '   ', '@', '@@bob', 'Şeki', 'bad-handle', 'has space',
        'a/b', 'x' * (USERNAME_CHARS + 1),
    ])
    def test_a_name_that_is_not_a_handle_raises_the_no_handle_rejection(self, page_name):
        with pytest.raises(MalformedMessage) as caught:
            handle_from_page(None, page_name)
        assert str(caught.value) == _NO_USABLE_HANDLE

    def test_exactly_one_leading_at_sign_comes_off(self):
        # Mirrors `api.schemas._HandleRequest._normalise_username`, which is
        # the authority: `@@bob` is left FAILING the charset check rather than
        # being guessed at.
        assert handle_from_page(None, '@bob') == 'bob'
        with pytest.raises(MalformedMessage):
            handle_from_page(None, '@@bob')

    def test_the_two_rejections_are_distinguishable(self):
        # A bad URL and an unusable name are different operator problems, and
        # the two rejection strings are the only signal that says which.
        assert _NOT_A_PROFILE_URL != _NO_USABLE_HANDLE


class TestMalformedMessageIsAValueError:
    """The ack policy catches `(MalformedMessage, ValidationError)` and acks.

    A `ValueError` subclass rather than a fresh hierarchy, because
    `pydantic.ValidationError` is itself a `ValueError` — so the two arrive
    together and a caller that only knows `ValueError` still catches this."""

    def test_it_is_a_value_error(self):
        assert issubclass(MalformedMessage, ValueError)

    def test_it_is_caught_by_the_policy_pair(self):
        with pytest.raises((MalformedMessage, ValidationError)):
            handle_from_page('https://evil.example/@bob', 'name')

    def test_pydantic_validation_error_is_a_value_error_too(self):
        assert issubclass(ValidationError, ValueError)


# ================================ 5: "every key present" is STRUCTURAL
class TestMetadataHasNoDefaults:
    """What makes "every key is ALWAYS present" a property of the type rather
    than a convention each call site remembers.

    Not one `Metadata` field carries a default. A defaulted field could be
    omitted at construction, and the outbound contract's promise — a consumer
    never needs a key-existence check — would then rest on `envelope.py`
    passing all seven every time."""

    FIELDS = ('keyword_id', 'keyword_name', 'page_id', 'page_name', 'scraped_at', 'source_term', 'profile')

    def complete(self, **over) -> dict:
        base = {'keyword_id': None, 'keyword_name': None, 'page_id': 1, 'page_name': 'sirabasc', 'scraped_at': '2026-09-10T08:40:39Z', 'source_term': 'search:sirabasc', 'profile': None}
        return {**base, **over}

    def test_the_field_set_is_exactly_the_contract(self):
        assert tuple(Metadata.model_fields) == self.FIELDS

    @pytest.mark.parametrize('field', FIELDS)
    def test_omitting_any_field_is_a_validation_error(self, field):
        payload = {key: value for key, value in self.complete().items() if key != field}
        with pytest.raises(ValidationError):
            Metadata(**payload)

    @pytest.mark.parametrize('field', FIELDS)
    def test_no_field_declares_a_default(self, field):
        assert Metadata.model_fields[field].is_required(), f'{field} gained a default; "every key present" is no longer structural'

    def test_a_complete_metadata_dumps_every_key_including_the_nulls(self):
        dumped = Metadata(**self.complete()).model_dump()
        assert tuple(dumped) == self.FIELDS
        assert dumped['keyword_id'] is None and dumped['profile'] is None


class TestSearchTypeNamesTheQueue:
    """`SearchType` is not `filters.SearchKind`: it answers "which queue did
    this result come from", which is why it has a `page` member and no
    `hashtag`/`user` ones."""

    def test_its_members_are_the_two_queues(self):
        assert [member.value for member in SearchType] == ['keyword', 'page']

    def test_it_serialises_as_the_plain_string(self):
        assert SearchType.PAGE.value == 'page'
        assert SearchType.KEYWORD.value == 'keyword'
