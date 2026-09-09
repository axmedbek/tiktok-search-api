"""Unit tests for `mapping.py` — the pure flattener layer.

`mapping.py` had only INCIDENTAL coverage before this file: three assertions
in `test_filters.py::TestMapping` on `flatten_video`/`flatten_user`, plus
key-tuple pins in `test_wiring_profile_posts_api.py::TestTheFlattenerContract`
reached through the app. Everything the profile epic added to the module —
`flatten_profile`, `_flag`, `_avatar_url`, `_verified`, and the two new
`sec_uid` keys in their ABSENT case — was untested here, where it is pure and
cheap to pin.

Three of these bite harder than the rest, and they are why the file exists:

1. **`_flag('0')` must be `False`.** `secret` is a numeric TikTok flag that a
   live reply may render as the STRING `'0'`, and `bool('0')` is `True` — so
   the bare-`bool()` version this helper replaced rendered a PUBLIC account as
   private, silently, invertedly, with no error anywhere. That inversion is
   invisible to every integration test built on `profile_node`, whose default
   `secret` is the int `0` (where `bool` and `_flag` agree).
2. **`heart_count` comes from `total_favorited`, not `favoriting_count`.**
   Likes RECEIVED versus likes GIVEN — two real keys, both plausible, one
   correct. A node carrying only `total_favorited` cannot tell them apart from
   each other in the direction that matters, so the pin here carries BOTH with
   different values.
3. **`_avatar_url`'s fallback ORDER.** `avatar_larger` → `medium` → `thumb` is
   a quality ordering; a reordered tuple still returns a working URL, so it
   only fails against a node whose three sizes carry DIFFERENT urls.

Pure functions, no fixtures, no network — conftest's session tripwire and the
per-test `_assert_no_network_attempted` still stand over them.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import profile_node  # noqa: E402

from tiktoksearch.mapping import (  # noqa: E402
    _AVATAR_KEYS,
    _avatar_url,
    _flag,
    _str_or_none,
    _verified,
    flatten_profile,
    flatten_user,
    flatten_video,
    to_int,
)

UID = '777'
SEC = 'SEC777'
HANDLE = 'bakuesaz'


# ===================================================== 1: the `_flag` table
class TestFlagIsNumericFirst:
    """`_flag`'s whole reason to exist: a numeric flag is COMPARED, never
    tested for truth. Then, and only then, a non-numeric value falls back to
    truthiness — which for `secret` errs toward private, the safe direction."""

    # The inversion the helper exists to prevent, first and alone: this is the
    # single case where `bool(value)` and `_flag(value)` DISAGREE on a value
    # TikTok really serves, and it is the case that leaks a public account's
    # privacy badge the wrong way round.
    def test_the_string_zero_is_false_where_bare_bool_says_true(self):
        assert _flag('0') is False
        assert bool('0') is True, 'the trap itself — if this ever changes, so did Python'

    # Every numeric spelling of the two flag values, in both types TikTok uses.
    @pytest.mark.parametrize('value', [0, '0', '00', 0.0, False, '  0  '])
    def test_numeric_zero_in_any_spelling_is_false(self, value):
        assert _flag(value) is False, value

    @pytest.mark.parametrize('value', [1, '1', 2, -1, True, 1.9, '  1  '])
    def test_a_non_zero_number_is_true(self, value):
        assert _flag(value) is True, value

    # A value with NO number to compare falls back to truthiness. Split by
    # direction, because the fallback's direction is the deliberate part.
    @pytest.mark.parametrize('value', [None, '', [], {}, ()])
    def test_an_empty_non_numeric_value_is_false(self, value):
        assert _flag(value) is False, value

    @pytest.mark.parametrize('value', ['false', 'true', 'yes', '0.0', ['x'],
                                       {'a': 1}, object()])
    def test_an_unparseable_value_fails_closed_to_true(self, value):
        # Deliberate: an unparseable `secret` is better shown as private than
        # leaked as public. `'0.0'` is here on purpose — `int('0.0')` raises,
        # so it is NOT a number to this helper and takes the truthy path.
        assert _flag(value) is True, value

    def test_the_return_is_always_a_real_bool(self):
        for value in (0, '0', 1, '1', None, '', 'x', [], 2.5):
            assert type(_flag(value)) is bool, value


# =============================================== 2: the coercion primitives
class TestToIntCoercion:
    @pytest.mark.parametrize('value,expected', [
        (5, 5), ('5', 5), ('  5  ', 5), (5.9, 5), (-3, -3), ('-3', -3),
        (True, 1), (False, 0), (0, 0), ('0', 0),
    ])
    def test_a_coercible_value_becomes_an_int(self, value, expected):
        assert to_int(value) == expected

    @pytest.mark.parametrize('value', [None, '', 'abc', '1.5', [], {}, object()])
    def test_an_uncoercible_value_is_none_not_zero(self, value):
        # None and not 0: a count that is ABSENT upstream must not be served
        # as the claim "this account has zero".
        assert to_int(value) is None, value


class TestStrOrNone:
    def test_only_none_maps_to_none(self):
        assert _str_or_none(None) is None
        # Everything else is stringified, INCLUDING falsy values — which is
        # what keeps a `uid` of 0 from vanishing into a null id.
        assert _str_or_none(0) == '0'
        assert _str_or_none('') == ''
        assert _str_or_none(False) == 'False'

    def test_a_non_string_id_is_stringified(self):
        assert _str_or_none(777) == '777'
        assert type(_str_or_none(777)) is str


class TestVerifiedIsTruthinessOnPurpose:
    """Unlike the numeric flags, these are string-or-absent fields where any
    non-empty string IS the verified semantic."""

    @pytest.mark.parametrize('node', [
        {'custom_verify': 'verified account'},
        {'enterprise_verify_reason': 'org'},
        {'custom_verify': '', 'enterprise_verify_reason': 'org'},
        {'custom_verify': 'x', 'enterprise_verify_reason': ''},
    ])
    def test_either_non_empty_field_verifies(self, node):
        assert _verified(node) is True, node

    @pytest.mark.parametrize('node', [
        {},
        {'custom_verify': ''},
        {'enterprise_verify_reason': ''},
        {'custom_verify': '', 'enterprise_verify_reason': ''},
        {'custom_verify': None, 'enterprise_verify_reason': None},
    ])
    def test_absent_or_empty_does_not_verify(self, node):
        assert _verified(node) is False, node

    def test_the_return_is_always_a_real_bool(self):
        assert type(_verified({})) is bool
        assert type(_verified({'custom_verify': 'x'})) is bool


# ================================================= 3: the avatar fallback
class TestAvatarUrlFallbackOrder:
    """Best size first. The order is only observable against a node whose
    three sizes carry DIFFERENT urls — with one shared url a reordered
    `_AVATAR_KEYS` returns the same string and the assertion is a no-op."""

    LARGE = 'https://cdn.test/large.jpeg'
    MEDIUM = 'https://cdn.test/medium.jpeg'
    THUMB = 'https://cdn.test/thumb.jpeg'

    def _node(self, *, larger=None, medium=None, thumb=None) -> dict:
        node: dict = {}
        for key, value in (('avatar_larger', larger), ('avatar_medium', medium),
                           ('avatar_thumb', thumb)):
            if value is not None:
                node[key] = value
        return node

    def test_the_key_order_is_largest_first(self):
        assert _AVATAR_KEYS == ('avatar_larger', 'avatar_medium', 'avatar_thumb')

    def test_all_three_present_takes_the_largest(self):
        node = self._node(larger={'url_list': [self.LARGE]},
                          medium={'url_list': [self.MEDIUM]},
                          thumb={'url_list': [self.THUMB]})
        assert _avatar_url(node) == self.LARGE

    def test_a_missing_larger_falls_through_to_medium(self):
        node = self._node(medium={'url_list': [self.MEDIUM]},
                          thumb={'url_list': [self.THUMB]})
        assert _avatar_url(node) == self.MEDIUM

    def test_only_thumb_present_is_still_served(self):
        assert _avatar_url(self._node(thumb={'url_list': [self.THUMB]})) == self.THUMB

    def test_an_empty_url_list_falls_through_rather_than_returning_none(self):
        # The distinction that matters: `avatar_larger` PRESENT with nothing in
        # it must not shadow a usable `avatar_medium`. A guard that returned on
        # the first present KEY rather than the first usable URL breaks here.
        node = self._node(larger={'url_list': []},
                          medium={'url_list': [self.MEDIUM]})
        assert _avatar_url(node) == self.MEDIUM

    def test_a_falsy_entry_inside_a_url_list_is_skipped(self):
        node = self._node(larger={'url_list': ['', None, self.LARGE]})
        assert _avatar_url(node) == self.LARGE

    def test_a_null_url_list_falls_through(self):
        node = self._node(larger={'url_list': None},
                          medium={'url_list': [self.MEDIUM]})
        assert _avatar_url(node) == self.MEDIUM

    def test_an_avatar_with_no_url_list_key_falls_through(self):
        node = self._node(larger={'uri': 'tos-abc'},
                          medium={'url_list': [self.MEDIUM]})
        assert _avatar_url(node) == self.MEDIUM

    @pytest.mark.parametrize('avatar', ['https://cdn.test/bare.jpeg', 0, 1,
                                        [], ['https://cdn.test/x.jpeg'], None])
    def test_a_non_dict_avatar_value_falls_through_without_raising(self, avatar):
        # The `isinstance(avatar, dict)` guard. Dropping it raises
        # AttributeError on a string avatar — a 500 out of a pure mapper.
        node = {'avatar_larger': avatar, 'avatar_medium': {'url_list': [self.MEDIUM]}}
        assert _avatar_url(node) == self.MEDIUM

    def test_no_avatar_anywhere_is_none(self):
        assert _avatar_url({}) is None
        assert _avatar_url({'avatar_larger': {'url_list': []}}) is None
        assert _avatar_url({'avatar_larger': 'nope'}) is None

    def test_a_non_string_url_is_stringified(self):
        assert _avatar_url({'avatar_larger': {'url_list': [12345]}}) == '12345'


# ================================================== 4: `flatten_profile`
class TestFlattenProfile:
    """The `/profile` mapper. It is served from a user-search node, so its
    two structurally-absent fields are part of the contract, not a bug."""

    # The exact tuple, ORDER INCLUDED. `api/app.py` builds the response as
    # `ProfileResponse(**record, …)`, so a renamed key here silently vanishes
    # under `extra='ignore'` and a key colliding with `source`/`device`/
    # `elapsed_s` is a 500 — both are loud here instead.
    KEYS = ('username', 'user_id', 'sec_uid', 'display_name', 'signature',
            'follower_count', 'following_count', 'aweme_count', 'heart_count',
            'region_code', 'verified', 'private', 'avatar_url')

    def test_a_populated_node_flattens_to_every_contract_key(self):
        record = flatten_profile(profile_node())
        assert record is not None
        assert tuple(record.keys()) == self.KEYS

    def test_a_populated_node_maps_its_values(self):
        record = flatten_profile(profile_node(
            unique_id=HANDLE, uid=UID, sec_uid=SEC, nickname='Baku Esaz',
            avatar='https://cdn.test/a.jpeg',
            follower_count='1000', following_count=10, aweme_count='50',
            total_favorited=41_921_593, secret=0, custom_verify='verified'))
        assert record == {
            'username': HANDLE, 'user_id': UID, 'sec_uid': SEC,
            'display_name': 'Baku Esaz', 'signature': None,
            'follower_count': 1000, 'following_count': 10, 'aweme_count': 50,
            'heart_count': 41_921_593, 'region_code': None,
            'verified': True, 'private': False,
            'avatar_url': 'https://cdn.test/a.jpeg'}

    def test_a_uid_less_node_flattens_to_none(self):
        # The guard, and the reason `client.profile` raises rather than serving
        # a 200 with a null profile.
        assert flatten_profile(profile_node(uid=None)) is None
        assert flatten_profile({}) is None
        assert flatten_profile({'unique_id': HANDLE, 'sec_uid': SEC}) is None

    def test_the_uid_guard_is_none_and_not_truthiness(self):
        # `if uid is None`, so a falsy-but-present uid still flattens — and
        # `str(uid)` then keeps it. A `if not uid` guard would drop this node
        # and turn a served profile into a 502.
        record = flatten_profile({'uid': 0})
        assert record is not None
        assert record['user_id'] == '0'

    def test_a_numeric_uid_is_stringified(self):
        record = flatten_profile({'uid': 777})
        assert record is not None
        assert record['user_id'] == '777'
        assert type(record['user_id']) is str

    # ---- the heart_count trap, with BOTH keys present ----
    def test_heart_count_reads_total_favorited_and_not_favoriting_count(self):
        # Likes RECEIVED vs likes GIVEN. Both keys are real and both are
        # plausible, so the ONLY arrangement that separates them is a node
        # carrying both with different values.
        record = flatten_profile(profile_node(total_favorited=41_921_593,
                                              favoriting_count=7))
        assert record is not None
        assert record['heart_count'] == 41_921_593
        assert record['heart_count'] != 7

    def test_heart_count_is_none_when_total_favorited_is_absent(self):
        # None and not 0: absent upstream must not be served as "no likes".
        record = flatten_profile(profile_node(total_favorited=None,
                                              favoriting_count=7))
        assert record is not None
        assert record['heart_count'] is None

    def test_heart_count_is_coerced_from_a_string(self):
        record = flatten_profile(profile_node(total_favorited='41921593'))
        assert record is not None
        assert record['heart_count'] == 41_921_593

    # ---- the private flag, through the mapper ----
    @pytest.mark.parametrize('secret,expected', [
        (0, False), ('0', False), (1, True), ('1', True), (None, False),
    ])
    def test_private_routes_through_flag(self, secret, expected):
        # `secret='0'` is the one that matters: a bare `bool()` here badges a
        # PUBLIC account private, and `profile_node`'s int-`0` default cannot
        # see it.
        record = flatten_profile(profile_node(secret=secret))
        assert record is not None
        assert record['private'] is expected

    # ---- the two interim nulls, at the mapper ----
    def test_signature_and_region_are_present_keys_holding_none(self):
        # Structurally absent from the user-search node, so null on this path.
        # They must be KEYS holding None, not missing keys: `ProfileResponse`
        # documents them as interim-null, and a dropped key would read as a
        # different contract.
        record = flatten_profile(profile_node())
        assert record is not None
        assert record['signature'] is None
        assert record['region_code'] is None

    def test_signature_and_region_are_served_when_upstream_does_carry_them(self):
        # The upgrade path: nothing in the mapper suppresses them, so a real
        # profile payload (or a future capture) flows straight through.
        record = flatten_profile(profile_node(signature='bio here', region='AZ'))
        assert record is not None
        assert record['signature'] == 'bio here'
        assert record['region_code'] == 'AZ'

    def test_the_counts_are_none_rather_than_zero_when_absent(self):
        record = flatten_profile({'uid': UID})
        assert record is not None
        for key in ('follower_count', 'following_count', 'aweme_count',
                    'heart_count'):
            assert record[key] is None, key
        assert record['sec_uid'] is None
        assert record['username'] is None
        assert record['avatar_url'] is None
        assert record['verified'] is False
        assert record['private'] is False

    def test_takes_no_source_term(self):
        # Unlike the other two flatteners: the `/profile` contract lists no
        # `source_term`, and a profile fetched by ids has no search term to
        # fabricate one from.
        with pytest.raises(TypeError):
            flatten_profile(profile_node(), 'search:x')
        assert 'source_term' not in self.KEYS


# ============================== 5: the two new sec_uid keys, ABSENT case
class TestSecUidIsMappedOnBothFlatteners:
    """`sec_uid` was mapped NOWHERE before this epic, and the posts endpoint
    keys on it. Presence is pinned through the app in
    `test_wiring_profile_posts_api.py`; the ABSENT case — where a naive
    `author['sec_uid']` raises and a mis-named key silently yields None
    forever — is pinned here."""

    def test_flatten_user_carries_sec_uid(self):
        record = flatten_user({'uid': '9', 'unique_id': 'nasa',
                               'sec_uid': SEC}, 'user:nasa')
        assert record is not None
        assert record['sec_uid'] == SEC

    def test_flatten_user_sec_uid_is_none_when_absent(self):
        record = flatten_user({'uid': '9', 'unique_id': 'nasa'}, 'user:nasa')
        assert record is not None
        assert record['sec_uid'] is None

    def test_flatten_user_sec_uid_is_stringified(self):
        record = flatten_user({'uid': 9, 'sec_uid': 12345}, 'user:x')
        assert record is not None
        assert record['sec_uid'] == '12345'
        assert record['user_id'] == '9'

    def test_flatten_video_carries_author_sec_uid(self):
        record = flatten_video({'aweme_id': '1',
                                'author': {'uid': UID, 'unique_id': HANDLE,
                                           'sec_uid': SEC}}, 'search:x')
        assert record is not None
        assert record['author_sec_uid'] == SEC

    def test_flatten_video_author_sec_uid_is_none_when_absent(self):
        record = flatten_video({'aweme_id': '1',
                                'author': {'uid': UID, 'unique_id': HANDLE}},
                               'search:x')
        assert record is not None
        assert record['author_sec_uid'] is None
        # And the sibling identity fields still resolve, so the None is about
        # `sec_uid` alone and not a broken author node.
        assert record['author_id'] == UID
        assert record['author_unique_id'] == HANDLE

    def test_flatten_video_with_no_author_at_all_nulls_the_author_fields(self):
        record = flatten_video({'aweme_id': '1'}, 'search:x')
        assert record is not None
        assert record['author_sec_uid'] is None
        assert record['author_id'] is None
        assert record['author_unique_id'] is None
        assert record['author_username'] is None
