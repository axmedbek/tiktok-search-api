"""Wiring tests for `/user/posts`' newest-first ordering.

The ordering is a CONTRACT (`UserPostsResponse.results` states it, and
`demo.html` accumulates-and-re-sorts on top of it), and until this file it was
entirely untested — for a reason worth recording, because it is the shape of
hole that repeats: `conftest.videos()` / `authored_videos()` built their
`aweme_info` with no `create_time`, so `mapping._iso_utc` returned None for
every fake, every `_create_time_key` was the unknown-date sentinel, and a
stable sort over all-equal keys is indistinguishable from no sort at all.
Three separate mutations of the sort passed 497/497 green: `reverse=True`
removed (feed comes back OLDEST-first), the `_newest_first` call deleted from
the handler, and `UNKNOWN_CREATE_TIME_KEY` changed from `''` to `'zzzz'`
(undated records lifted to the FRONT — the exact fabrication, "this is the
most recent post", that the sentinel's own docstring says it exists to
prevent). The conftest builders now stamp a `create_time`, which is what makes
everything below able to fail.

Three things shape the assertions:

1. **The string sort key's soundness is pinned as a property of
   `mapping._iso_utc`, not assumed.** `_create_time_key` sorts ISO-8601
   STRINGS rather than parsed datetimes, and that is only correct because
   every rendered timestamp has equal width, zero-padded fields and the same
   `+00:00` offset. So the 25-character form is asserted across the whole
   reachable `datetime` range — negative epochs included — together with
   epoch-order == string-order. A `Z` suffix, or a fractional `ts` reaching
   `_iso_utc`, silently breaks the ordering everywhere, with no error.
2. **A tie group is arranged so the sort MOVES something.** An assertion whose
   pre-sort order already equals the sorted order is the same no-op that
   blinded the 497, so every ordering test here starts from a page that is
   NOT already sorted.
3. **The impostor is arranged as the NEWEST record.** The existing impostor
   test (`test_wiring_profile_posts_api.py`) does not constrain the sort; this
   is the arrangement where ordering decides index 0, on a terminal page so
   `page_token` is null and "absent from the body" means every byte.

Everything is stubbed at `requests.Session.get` and driven through the real
app; `signer: local` keeps the paid-signer ledger at zero. Nothing leaves the
process (conftest's tripwire).

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    DEFAULT_CREATE_TIME,
    AsgiClient,
    FakeTransport,
    author_node,
    authored_reply,
    drive,
    identity,
    reply,
    write_config,
    write_identities,
)

from tiktoksearch.api import app as app_module  # noqa: E402
from tiktoksearch.api.app import (  # noqa: E402
    UNKNOWN_CREATE_TIME_KEY,
    _create_time_key,
    _newest_first,
)
from tiktoksearch.client import SEARCH_ITEM_PATH, SEARCH_VIDEO_PATH  # noqa: E402
from tiktoksearch.mapping import _iso_utc, flatten_video, to_int  # noqa: E402

DEVICE_A = 'DEVA'
HANDLE = 'bakuesaz'

# The genuine account, and the nickname-only impostor whose ids must never be
# served as this handle's. Same shapes as test_wiring_profile_posts_api.py.
GENUINE_UID = '777'
GENUINE_SEC = 'SEC777'
GENUINE = author_node(unique_id=HANDLE, nickname='Baku Esaz',
                      uid=GENUINE_UID, sec_uid=GENUINE_SEC)
IMPOSTOR_UID = '666'
IMPOSTOR_SEC = 'SEC666'
IMPOSTOR = author_node(nickname=HANDLE, uid=IMPOSTOR_UID, sec_uid=IMPOSTOR_SEC)

# Four well-separated epochs, one of them PRE-EPOCH. The negative is not a
# curiosity: `to_int('-1')` is -1, so a negative upstream `create_time` reaches
# `_iso_utc` and renders — and it is the case where a naive key (a bare int
# rendered as a string, say) would mis-order.
T_2023 = 1_700_000_000
T_2020 = 1_600_000_000
T_2001 = 1_000_000_000
T_1969 = -1

# The ISO-8601 form the whole string-key premise rests on: fixed 25 characters,
# zero-padded throughout, always the `+00:00` offset (never `Z`).
ISO_UTC_WIDTH = 25
ISO_UTC_FORM = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$')
# The reachable ends of `datetime.fromtimestamp(ts, tz=utc)`: datetime.min and
# datetime.max in UTC. One second outside either raises ValueError.
MIN_TS = -62_135_596_800
MAX_TS = 253_402_300_799


# ------------------------------------------------------------------ harness
def _app(tmp_path: Path, **config_over):
    """The real app over a synthetic identities file, in `signer: local` mode."""
    ids_path = tmp_path / 'ids.json'
    write_identities(ids_path, [identity(DEVICE_A)], stamp=1_700_000_100)
    config_path = tmp_path / 'config.yaml'
    write_config(config_path, signer='local', **config_over)
    return app_module.create_app(str(config_path))


def _post(app, path: str, *payloads) -> list[tuple[int, dict]]:
    """POST each payload inside ONE app lifespan, so a later call meets the
    pool state and device the earlier one left behind."""
    async def sequence():
        async with AsgiClient(app) as client:
            return [await client.post(path, payload) for payload in payloads]

    return drive(sequence())


def _by_path(transport: FakeTransport, mapping) -> None:
    """Answer each signed path from `mapping`. A keyword search drives BOTH
    merged video endpoints, so a page normally needs an answer for each."""
    def handler(call):
        answer = mapping[call['path']]
        return answer(call) if callable(answer) else json.loads(json.dumps(answer))

    transport.script(handler)


def _single_page(transport: FakeTransport, items, create_times) -> None:
    """One posts page, TERMINAL on both merged endpoints so no token is minted.

    The Videos-tab endpoint echoes the SAME items, which the shared dedup
    window drops — so the page's records are exactly `items`, IN THE ORDER
    given, which is what makes "the sort reordered them" observable."""
    _by_path(transport, {
        SEARCH_VIDEO_PATH: authored_reply(items, cursor=len(items),
                                          has_more=False,
                                          create_times=create_times),
        SEARCH_ITEM_PATH: authored_reply(items, cursor=len(items),
                                         has_more=False,
                                         key='search_item_list',
                                         create_times=create_times),
    })


def _authored(dated) -> tuple[list, dict]:
    """`(items, create_times)` for a page of GENUINE records.

    `dated` is an iterable of `(aweme_id, create_time)` pairs, in the order
    upstream serves them — a `create_time` of None drops the key (an undated
    record)."""
    pairs = [(str(i), t) for i, t in dated]
    return ([(i, GENUINE) for i, _ in pairs], {i: t for i, t in pairs})


def _posts(app, transport, payload=None) -> tuple[int, dict]:
    return _post(app, '/user/posts', payload or {'username': f'@{HANDLE}'})[0]


def _ids(body: dict) -> list[str]:
    return [record['id'] for record in body['results']]


def _times(body: dict) -> list[str | None]:
    return [record['create_time'] for record in body['results']]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# A deterministic spread over the whole reachable range, plus both ends, both
# sides of the epoch, and the decade boundaries a width bug would hide behind
# (year < 1000 is where zero-padding is load-bearing).
def _samples() -> list[int]:
    fixed = [MIN_TS, MIN_TS + 1, -62_135_510_400, -2_208_988_800, -86_400, -1,
             1, 86_400, T_2001, T_2020, T_2023, MAX_TS - 1, MAX_TS]
    powers = [sign * 10 ** exp for exp in range(1, 12) for sign in (1, -1)]
    rng = random.Random(20_260_909)
    spread = [rng.randint(MIN_TS, MAX_TS) for _ in range(2_000)]
    return [ts for ts in fixed + powers + spread if MIN_TS <= ts <= MAX_TS and ts]


SAMPLES = _samples()


# ============================================ 1: the string-sort-key premise
class TestTheLexicographicPremiseOfTheStringSortKey:
    """`_create_time_key` sorts STRINGS, and its docstring claims that is sound
    rather than lucky because `mapping._iso_utc` renders a fixed 25-character
    `YYYY-MM-DDTHH:MM:SS+00:00`. That claim is the foundation everything else
    here stands on, so it is asserted as a property over the whole reachable
    range instead of taken on trust: equal width + zero-padded fields + one
    fixed offset is exactly what makes lexicographic order chronological."""

    def test_every_rendered_timestamp_is_the_fixed_25_char_form(self):
        # Catches a `Z` suffix (`...:20Z` is 20 chars and has no `+00:00`), a
        # dropped offset, and any non-padded field — none of which raises, and
        # all of which silently mis-order the feed.
        #
        # `SAMPLES` is truthy and IN range by construction, and that is the
        # exact domain the width guarantee is claimed over: a `ts` outside it
        # renders as None (see below), which is a legitimate return and NOT a
        # 25-character string. So the width is asserted unconditionally here —
        # a None slipping into this set would fail the isinstance check rather
        # than be tolerated — because it is what makes the string sort key
        # sound for every value that reaches the key at all.
        assert len(SAMPLES) > 1_000
        assert all(ts and MIN_TS <= ts <= MAX_TS for ts in SAMPLES)
        for ts in SAMPLES:
            rendered = _iso_utc(ts)
            assert isinstance(rendered, str), ts
            assert len(rendered) == ISO_UTC_WIDTH, (ts, rendered)
            assert ISO_UTC_FORM.match(rendered), (ts, rendered)

    def test_epoch_order_equals_string_order_on_adjacent_pairs(self):
        # THE property. Sorting the rendered strings must be the same
        # permutation as sorting the epochs they came from.
        ordered = sorted(set(SAMPLES))
        for earlier, later in zip(ordered, ordered[1:]):
            assert _iso_utc(earlier) < _iso_utc(later), (earlier, later)

    def test_string_sorting_the_samples_reproduces_epoch_sorting(self):
        # The same property stated as the operation the handler performs, so a
        # pathological non-adjacent pair cannot slip through either.
        unique = sorted(set(SAMPLES))
        assert sorted(unique, key=_iso_utc) == unique

    def test_both_datetime_boundaries_render_in_the_same_form(self):
        # The extremes are where a width assumption breaks first: year 1 needs
        # `isoformat`'s zero-padding to stay 4 digits.
        assert _iso_utc(MIN_TS) == '0001-01-01T00:00:00+00:00'
        assert _iso_utc(MAX_TS) == '9999-12-31T23:59:59+00:00'
        assert len(_iso_utc(MIN_TS)) == len(_iso_utc(MAX_TS)) == ISO_UTC_WIDTH

    def test_a_year_below_1000_still_sorts_below_a_modern_timestamp(self):
        # If the year were not zero-padded, '1-01-02...' would compare GREATER
        # than '2023-...' and the oldest record on earth would sort newest.
        ancient = _iso_utc(-62_135_510_400)
        assert ancient == '0001-01-02T00:00:00+00:00'
        assert ancient < _iso_utc(T_2001)

    def test_a_negative_epoch_is_reachable_and_orders_correctly(self):
        # Reachable, not hypothetical: `to_int` passes a negative straight
        # through, so a pre-1970 `create_time` renders and must sort oldest.
        assert to_int('-1') == -1
        assert _iso_utc(T_1969) == '1969-12-31T23:59:59+00:00'
        assert _iso_utc(T_1969) < _iso_utc(1)
        assert _iso_utc(T_1969) < _iso_utc(T_2001)

    def test_outside_the_datetime_range_iso_utc_degrades_to_the_unknown_date(self):
        # The honest edge of the property above: one second past either end of
        # `datetime` there is no renderable date, so `create_time` degrades to
        # None exactly like an absent `music_title` — the module's standing
        # rule that a presentation field never raises out of the flattening
        # layer. It used to raise ValueError uncaught (`flatten_video` does not
        # catch, `pool.run_call` does not, `_domain_errors` maps only the
        # domain classes), which escaped `run_in_executor` as a 500; the
        # end-to-end pin of the served record is in section 6 below.
        # `to_int` passes an arbitrarily large int straight through, so the
        # OverflowError shape (beyond the platform's `time_t`) is reachable
        # from upstream data too, not only the ValueError one.
        for out_of_range in (MAX_TS + 1, MIN_TS - 1, 9_999_999_999_999,
                             10 ** 30, -(10 ** 30)):
            assert _iso_utc(out_of_range) is None, out_of_range
        # And degrading is not the same as widening the accepted range: the
        # last renderable second on each side still renders.
        assert _iso_utc(MAX_TS) == '9999-12-31T23:59:59+00:00'
        assert _iso_utc(MIN_TS) == '0001-01-01T00:00:00+00:00'

    def test_an_out_of_range_create_time_flattens_to_a_None_create_time(self):
        # Through `flatten_video`, so the degradation is a property of the
        # record and not only of the private helper: the record is still
        # SERVED (its id and stats survive), it just carries no date.
        record = flatten_video({'aweme_id': '1', 'create_time': MAX_TS + 1}, 's')
        assert record is not None
        assert record['id'] == '1'
        assert record['create_time'] is None
        # Which feeds the sentinel key, i.e. it sorts last under `reverse=True`.
        assert _create_time_key(record) == UNKNOWN_CREATE_TIME_KEY

    def test_a_falsy_timestamp_is_the_unknown_date_None(self):
        # `_iso_utc`'s `if ts` guard: this is the ONLY source of a None
        # `create_time`, and therefore of the sentinel key.
        assert _iso_utc(0) is None
        assert _iso_utc(None) is None
        assert to_int('') is None
        assert to_int('not-a-number') is None

    def test_the_unknown_date_sentinel_sorts_below_every_rendered_timestamp(self):
        # What makes `reverse=True` put undated records LAST rather than first.
        assert UNKNOWN_CREATE_TIME_KEY < _iso_utc(MIN_TS)
        for ts in SAMPLES[:200]:
            assert UNKNOWN_CREATE_TIME_KEY < _iso_utc(ts), ts

    def test_flatten_video_keeps_the_width_premise_on_a_fractional_create_time(self):
        # The premise holds because `flatten_video` routes `create_time`
        # through `to_int`. Drop that coercion and a float upstream value
        # renders 32 characters with a fractional part — see the test below.
        for raw in (1_700_000_000.5, 1_700_000_000.999999, '1700000000',
                    True, 1_700_000_000):
            record = flatten_video({'aweme_id': '1', 'create_time': raw}, 's')
            assert record is not None
            assert len(record['create_time']) == ISO_UTC_WIDTH, raw
            assert ISO_UTC_FORM.match(record['create_time']), raw

    def test_a_fractional_timestamp_reaching_iso_utc_breaks_the_width(self):
        # Pins WHY the `to_int` hop above is load-bearing rather than
        # decorative: a float widens the render to 32 chars, and a 32-char key
        # compares against 25-char keys by prefix — mis-ordering with no error.
        widened = _iso_utc(1_700_000_000.5)
        assert widened == '2023-11-14T22:13:20.500000+00:00'
        assert len(widened) != ISO_UTC_WIDTH
        # And it demonstrably mis-orders: a fractionally LATER timestamp sorts
        # BELOW the whole second before it.
        assert _iso_utc(1_700_000_000.5) > _iso_utc(1_700_000_000)
        assert _iso_utc(1_699_999_999.5) < _iso_utc(1_700_000_000)


# ================================================== 6: the key on odd values
class TestTheSortKeyOfANonStringValue:
    """`_create_time_key` must return a STRING for every input, or `sorted`
    compares a `str` against an `int` and the handler 500s. The guard is
    `isinstance(value, str) and value`; weakening it to `value if value else ''`
    keeps every one-type page green and raises TypeError on a mixed one."""

    def test_a_missing_key_is_the_sentinel(self):
        assert _create_time_key({}) == UNKNOWN_CREATE_TIME_KEY
        assert _create_time_key({'id': '1'}) == UNKNOWN_CREATE_TIME_KEY

    def test_every_non_string_value_is_the_sentinel_and_is_a_string(self):
        for value in (None, '', 0, 123, -1, 1.5, [], {}, (), False, True,
                      ['2023-11-14T22:13:20+00:00']):
            key = _create_time_key({'create_time': value})
            assert key == UNKNOWN_CREATE_TIME_KEY, value
            assert isinstance(key, str), value

    def test_a_non_empty_string_is_returned_unchanged(self):
        stamp = _iso(T_2023)
        assert _create_time_key({'create_time': stamp}) == stamp

    def test_a_page_mixing_strings_and_non_strings_sorts_without_raising(self):
        # The failure mode in one shot: under the weakened guard this raises
        # `TypeError: '<' not supported between instances of 'str' and 'int'`.
        page = [{'id': 'int', 'create_time': 123},
                {'id': 'dated', 'create_time': _iso(T_2023)},
                {'id': 'list', 'create_time': []},
                {'id': 'older', 'create_time': _iso(T_2001)}]
        out = _newest_first(page)
        assert [record['id'] for record in out] == ['dated', 'older',
                                                    'int', 'list']


# ============================================ 2: the page is newest-first
class TestThePageIsOrderedNewestFirst:
    """The ordering contract, on the response body. Upstream serves the page
    UNSORTED, so the assertion cannot pass on "no sort" — which is what the
    dateless fakes made every prior ordering assertion do."""

    # Upstream order is deliberately shuffled: 2001, 2023, 1969, 2020.
    UPSTREAM = (('1', T_2001), ('2', T_2023), ('3', T_1969), ('4', T_2020))
    # Newest-first: 2023, 2020, 2001, 1969.
    EXPECTED = ['2', '4', '1', '3']

    def test_results_come_back_strictly_newest_first(self, tmp_path, transport,
                                                     rapid_ledger):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 4
        # The sort MOVED records: upstream order was 1,2,3,4.
        assert _ids(body) == self.EXPECTED
        assert _ids(body) != ['1', '2', '3', '4']
        served = _times(body)
        assert served == [_iso(T_2023), _iso(T_2020), _iso(T_2001), _iso(T_1969)]
        # Strictly descending as strings AND as the epochs they encode, so the
        # assertion does not rest on the string premise it is testing.
        assert served == sorted(served, reverse=True)
        assert all(a > b for a, b in zip(served, served[1:]))
        parsed = [datetime.fromisoformat(stamp) for stamp in served]
        assert all(a > b for a, b in zip(parsed, parsed[1:]))
        assert rapid_ledger == []

    def test_a_page_served_oldest_first_upstream_is_reversed(self, tmp_path,
                                                             transport):
        # The pure `reverse=True` case: upstream ascending, response must be
        # descending. Deleting `reverse=True` returns the upstream order here.
        items, times = _authored((('1', T_1969), ('2', T_2001),
                                  ('3', T_2020), ('4', T_2023)))
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert _ids(body) == ['4', '3', '2', '1']

    def test_the_default_dated_fake_still_yields_a_real_timestamp(self, tmp_path,
                                                                  transport):
        # The conftest change itself, pinned: a builder that stops stamping
        # `create_time` makes every ordering test above vacuous again, so its
        # default must fail loudly here rather than silently disarm them.
        items, times = _authored((('1', DEFAULT_CREATE_TIME),))
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert _times(body) == [_iso(DEFAULT_CREATE_TIME)]
        assert len(body['results'][0]['create_time']) == ISO_UTC_WIDTH


# ================================================ 3: the unknown-date tail
class TestUndatedRecordsGoToTheTail:
    """A record with no date cannot be truthfully placed among dated ones, and
    the FRONT of a newest-first list is the response's strongest claim — an
    undated record there asserts "this is the most recent post", which is a
    fabrication. `UNKNOWN_CREATE_TIME_KEY = ''` is what puts them last; change
    it to any string above `'0'` and they lead the feed."""

    # `create_time` absent AND `create_time: 0` are BOTH undated: `_iso_utc`'s
    # `if ts` guard renders a zero as None, so the two upstream shapes must
    # reach the same place. Dated records are shuffled, so the sort must work.
    UPSTREAM = (('1', T_2001), ('2', None), ('3', T_2023), ('4', 0),
                ('5', T_2020))
    UNDATED = ('2', '4')

    def test_every_dated_record_precedes_every_undated_one(self, tmp_path,
                                                           transport):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 5
        served = _times(body)
        dated = [i for i, stamp in enumerate(served) if stamp is not None]
        undated = [i for i, stamp in enumerate(served) if stamp is None]
        assert dated and undated
        assert max(dated) < min(undated)
        # The exact shape, so "the tail" is not merely "somewhere later".
        assert _ids(body) == ['3', '5', '1', '2', '4']

    def test_the_dated_prefix_is_strictly_descending(self, tmp_path, transport):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        _, body = _posts(_app(tmp_path), transport)
        prefix = [stamp for stamp in _times(body) if stamp is not None]
        assert prefix == [_iso(T_2023), _iso(T_2020), _iso(T_2001)]
        assert all(a > b for a, b in zip(prefix, prefix[1:]))

    def test_the_undated_tail_keeps_the_upstream_order(self, tmp_path, transport):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        _, body = _posts(_app(tmp_path), transport)
        tail = [record['id'] for record in body['results']
                if record['create_time'] is None]
        assert tail == list(self.UNDATED)

    def test_an_all_undated_page_keeps_the_upstream_order_untouched(
            self, tmp_path, transport):
        # No date anywhere is the state the whole pre-existing suite was in:
        # every key ties, so the only correct answer is the upstream order.
        items, times = _authored((('1', None), ('2', 0), ('3', None)))
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert _ids(body) == ['1', '2', '3']
        assert _times(body) == [None, None, None]

    def test_a_single_undated_record_is_not_promoted_over_a_dated_one(
            self, tmp_path, transport):
        # The minimal fabrication case: undated FIRST upstream, so a sentinel
        # that compares high leaves it at index 0 and the response claims an
        # undated record is the account's newest post.
        items, times = _authored((('1', None), ('2', T_2001)))
        _single_page(transport, items, times)
        _, body = _posts(_app(tmp_path), transport)
        assert _ids(body) == ['2', '1']
        assert body['results'][0]['create_time'] == _iso(T_2001)
        assert body['results'][-1]['create_time'] is None


# ==================================================== 4: stability
class TestTheSortIsStable:
    """Records sharing a timestamp keep the order upstream gave them. That is
    what makes two identical requests return identical orderings, which the
    UI's accumulate-and-re-sort and any client caching depend on.

    The tie group is arranged so the sort MOVES something — a page that is
    already in sorted order cannot tell a stable sort from an absent one, and
    that is precisely the blind spot this file exists to close."""

    # '10' and '11' tie; '12' is newer and sits LAST upstream, so the sort must
    # move it to the front while leaving the tie group's order alone.
    UPSTREAM = (('10', T_2020), ('11', T_2020), ('12', T_2023))
    EXPECTED = ['12', '10', '11']

    def test_a_tie_group_keeps_the_upstream_order_while_the_sort_moves(
            self, tmp_path, transport):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        # The sort is NOT a no-op on this page …
        assert _ids(body) != ['10', '11', '12']
        # … and within the tie group the upstream order survives. A secondary
        # sort key (`(_create_time_key(r), r['id'])`, say) reverses it to
        # 11, 10 under `reverse=True`.
        assert _ids(body) == self.EXPECTED
        assert _times(body) == [_iso(T_2023), _iso(T_2020), _iso(T_2020)]

    def test_a_tie_group_served_in_reverse_id_order_also_survives(
            self, tmp_path, transport):
        # The same page with the tie group's ids swapped. Together the two
        # tests pin STABILITY rather than an id ordering that happens to match:
        # any id-derived tiebreak now fails one of them.
        items, times = _authored((('11', T_2020), ('10', T_2020),
                                  ('12', T_2023)))
        _single_page(transport, items, times)
        _, body = _posts(_app(tmp_path), transport)
        assert _ids(body) == ['12', '11', '10']

    def test_two_identical_requests_return_identical_id_sequences(
            self, tmp_path, transport):
        items, times = _authored(self.UPSTREAM)
        _single_page(transport, items, times)
        payload = {'username': f'@{HANDLE}'}
        (first_status, first), (second_status, second) = _post(
            _app(tmp_path), '/user/posts', payload, payload)
        assert first_status == second_status == 200
        assert _ids(first) == _ids(second) == self.EXPECTED
        assert _times(first) == _times(second)

    def test_a_whole_undated_tie_group_keeps_its_upstream_order_behind_dates(
            self, tmp_path, transport):
        # The undated tail is one big tie group, so stability governs it too.
        items, times = _authored((('1', None), ('2', T_2001), ('3', None),
                                  ('4', T_2023), ('5', None)))
        _single_page(transport, items, times)
        _, body = _posts(_app(tmp_path), transport)
        assert _ids(body) == ['4', '2', '1', '3', '5']


# ================================== 5: the impostor as the NEWEST record
class TestTheImpostorAsTheNewestRecord:
    """The impersonation guard re-run in the arrangement where ORDERING decides
    index 0 and the reported ids.

    The existing impostor test lists the impostor first in a page where nothing
    is dated, so the sort cannot move it and the test constrains the filter
    alone. Here the impostor is the NEWEST record: filter and sort must BOTH
    hold, because `_posts_ids` reads the first record of the sorted, filtered
    list — so a filter that let the impostor through would hand back ITS ids as
    this handle's, from index 0, with every key present and well-typed.

    The page is terminal, so `page_token` is null and "absent from the body"
    really is every byte the caller receives."""

    # The impostor is NEWEST; the genuine records are older and shuffled.
    UPSTREAM = (('1', IMPOSTOR, T_2023), ('2', GENUINE, T_2001),
                ('3', GENUINE, T_2020))

    def _page(self, transport) -> None:
        items = [(str(i), author) for i, author, _ in self.UPSTREAM]
        times = {str(i): t for i, _, t in self.UPSTREAM}
        _single_page(transport, items, times)

    def test_the_newest_impostor_is_absent_from_the_whole_body(
            self, tmp_path, transport, rapid_ledger):
        self._page(transport)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 2
        # Newest-first among the GENUINE records only.
        assert _ids(body) == ['3', '2']
        assert _times(body) == [_iso(T_2020), _iso(T_2001)]
        assert body['page_token'] is None
        serialised = json.dumps(body)
        assert IMPOSTOR_UID not in serialised
        assert IMPOSTOR_SEC not in serialised
        assert rapid_ledger == []

    def test_the_reported_ids_are_the_genuine_accounts(self, tmp_path,
                                                       transport):
        # `_posts_ids` reads the FIRST record of the sorted+filtered list, so
        # this is the assertion the ordering actually decides.
        self._page(transport)
        _, body = _posts(_app(tmp_path), transport)
        assert body['user_id'] == GENUINE_UID
        assert body['sec_uid'] == GENUINE_SEC
        assert body['results'][0]['author_id'] == GENUINE_UID
        assert body['results'][0]['author_sec_uid'] == GENUINE_SEC

    def test_the_newest_impostor_does_not_take_index_zero(self, tmp_path,
                                                          transport):
        # Stated as the user-visible claim: index 0 is "the account's most
        # recent post", and it must be a post the account authored — even when
        # a foreign record carries a newer date.
        self._page(transport)
        _, body = _posts(_app(tmp_path), transport)
        first = body['results'][0]
        assert first['id'] == '3'
        assert first['author_unique_id'] == HANDLE
        assert first['create_time'] == _iso(T_2020)
        # The impostor's date was newer than everything served.
        assert first['create_time'] < _iso(T_2023)

    def test_an_impostor_alone_on_a_dated_page_still_yields_nothing(
            self, tmp_path, transport):
        # Kept beside the mixed page above: on its own it cannot separate a
        # right answer from an endpoint that returns nothing, but it does pin
        # that a date does not buy an unmatched record a place in the feed.
        items = [('1', IMPOSTOR)]
        _single_page(transport, items, {'1': T_2023})
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 0
        assert body['results'] == []
        assert body['user_id'] is None
        assert body['sec_uid'] is None
        assert IMPOSTOR_UID not in json.dumps(body)


# ======================================= 7: /search ordering is NOT touched
class TestSearchOrderingIsNotTouched:
    """`/search` returns RELEVANCE order, and that is a separate contract.

    No test in the suite carried a `create_time` on a search page before this
    file, so someone adding `_newest_first` to the `/search` handler — a
    plausible "make it consistent" edit — would have passed green while
    silently re-ordering every search caller's results."""

    # Descending, then ascending: the sequence is not sorted in EITHER
    # direction, so neither `_newest_first` nor its reverse can reproduce it.
    UPSTREAM = (('1', T_2023), ('2', T_2020), ('3', T_1969), ('4', T_2001),
                ('5', T_2023))

    def _page(self, transport) -> None:
        ids = [i for i, _ in self.UPSTREAM]
        times = {i: t for i, t in self.UPSTREAM}
        _by_path(transport, {
            SEARCH_VIDEO_PATH: reply(ids, cursor=len(ids), has_more=False,
                                     create_times=times),
            SEARCH_ITEM_PATH: reply(ids, cursor=len(ids), has_more=False,
                                    key='search_item_list', create_times=times),
        })

    def _search(self, tmp_path, transport) -> tuple[int, dict]:
        self._page(transport)
        return _post(_app(tmp_path), '/search',
                     {'type': 'keyword', 'query': 'ocean', 'limit': 10})[0]

    def test_search_results_stay_in_upstream_relevance_order(self, tmp_path,
                                                             transport):
        status, body = self._search(tmp_path, transport)
        assert status == 200
        assert body['count'] == 5
        assert _ids(body) == ['1', '2', '3', '4', '5']

    def test_the_search_page_really_is_unsorted_by_date(self, tmp_path,
                                                        transport):
        # Guards the guard: if the fixture's dates were monotonic, the test
        # above would pass with `_newest_first` bolted onto `/search`.
        _, body = self._search(tmp_path, transport)
        served = _times(body)
        assert served == [_iso(T_2023), _iso(T_2020), _iso(T_1969),
                          _iso(T_2001), _iso(T_2023)]
        assert served != sorted(served, reverse=True)
        assert served != sorted(served)
        assert not all(a >= b for a, b in zip(served, served[1:]))

    def test_the_posts_endpoint_sorts_the_same_page_that_search_does_not(
            self, tmp_path, transport):
        # The two contracts side by side on ONE upstream page, so "posts sorts,
        # search does not" is a single observable difference rather than two
        # tests that could drift apart.
        ids = [i for i, _ in self.UPSTREAM]
        times = {i: t for i, t in self.UPSTREAM}
        _single_page(transport, [(i, GENUINE) for i in ids], times)
        _, posts = _posts(_app(tmp_path), transport)
        assert _ids(posts) == ['1', '5', '2', '4', '3']
        _, search = self._search(tmp_path, transport)
        assert _ids(search) == ids


# ======================= 5: the sort made `_posts_ids` nullability positional
class TestSortingMadeTheReportedSecUidOrderDependent:
    """`_posts_ids` reads `records[0]`, NOT the first record with a non-null
    id — and the handler hands it `_newest_first(_authored_by(...))`, so the
    NEWEST kept record now decides the reported pair.

    Consequence, and it is the whole class: a page whose newest record carries
    no `author_sec_uid` reports `sec_uid: null` where the same page in
    upstream order would have reported a real value. Never a WRONG account —
    every record here already matched the handle on the identity field, so the
    permutation guarantee holds and only NULLABILITY moved.

    These tests pin the CURRENT behaviour, deliberately. They are named to
    fail loudly if `_posts_ids` is changed to take the first non-null id
    (which is the open follow-up recorded against subtask 7), so the change is
    made and re-pinned on purpose rather than discovered by a client."""

    # Same genuine handle throughout — only `sec_uid` differs, so nothing here
    # is about the author filter.
    NEWEST_NO_SEC = author_node(unique_id=HANDLE, nickname='Baku Esaz',
                                uid=GENUINE_UID, sec_uid=None)
    OLDER_WITH_SEC = GENUINE

    def _page(self, transport, items, times) -> None:
        _single_page(transport, items, times)

    def test_CURRENT_a_newest_record_without_sec_uid_reports_null_sec_uid(
            self, tmp_path, transport):
        # Upstream order puts the DATED-OLDER record (which HAS a sec_uid)
        # first, so before the sort landed this page reported GENUINE_SEC.
        # After it, the undated-by-sec newest record wins and the value is
        # null. If this assertion starts failing, `_posts_ids` was fixed —
        # update it to expect GENUINE_SEC.
        items = [('1', self.OLDER_WITH_SEC), ('2', self.NEWEST_NO_SEC)]
        self._page(transport, items, {'1': T_2001, '2': T_2023})
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 2
        # The sort MOVED something: newest ('2') is now index 0.
        assert _ids(body) == ['2', '1']
        # ...and that is what decides the reported pair.
        assert body['sec_uid'] is None
        assert body['user_id'] == GENUINE_UID
        # The real value IS on a record in the very same response body, which
        # is what makes the null a positional artefact and not missing data.
        assert body['results'][1]['author_sec_uid'] == GENUINE_SEC
        assert body['results'][0]['author_sec_uid'] is None

    def test_the_same_page_dated_the_other_way_round_reports_the_sec_uid(
            self, tmp_path, transport):
        # The control, and the proof that the null above is ORDERING and not
        # the record set: identical records, identical filter, only the two
        # `create_time`s swapped — and the reported `sec_uid` changes.
        items = [('1', self.OLDER_WITH_SEC), ('2', self.NEWEST_NO_SEC)]
        self._page(transport, items, {'1': T_2023, '2': T_2001})
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert _ids(body) == ['1', '2']
        assert body['sec_uid'] == GENUINE_SEC
        assert body['user_id'] == GENUINE_UID

    # DISTINCT from the record's own ids on purpose. Supplying GENUINE_SEC
    # here would make "the caller's value won" and "the record's value won"
    # produce the same byte, so the assertion could not tell them apart — the
    # tie-order no-op in another costume.
    PINNED_UID = '111'
    PINNED_SEC = 'SEC111'

    def test_a_supplied_sec_uid_is_immune_to_the_ordering(self, tmp_path,
                                                          transport):
        # The documented mitigation: what the caller supplied wins, so a
        # client that keeps the ids from `/profile` never sees the artefact.
        items = [('1', self.OLDER_WITH_SEC), ('2', self.NEWEST_NO_SEC)]
        self._page(transport, items, {'1': T_2001, '2': T_2023})
        status, body = _posts(_app(tmp_path), transport, {
            'username': f'@{HANDLE}', 'user_id': self.PINNED_UID,
            'sec_uid': self.PINNED_SEC})
        assert status == 200
        assert _ids(body) == ['2', '1']
        # The caller's values, and NOT the ones sitting on the records — which
        # is only observable because the two differ.
        assert body['sec_uid'] == self.PINNED_SEC
        assert body['user_id'] == self.PINNED_UID
        assert body['results'][1]['author_sec_uid'] == GENUINE_SEC
        assert body['results'][0]['author_id'] == GENUINE_UID

    def test_the_reported_ids_always_come_from_a_record_that_matched(
            self, tmp_path, transport):
        # The guarantee that DID survive the sort, asserted against the
        # impostor: whichever record ends up at index 0, the ids reported are
        # never a foreign account's. The impostor is the NEWEST record here —
        # the arrangement where ordering decides index 0.
        items = [('1', GENUINE), ('2', IMPOSTOR)]
        self._page(transport, items, {'1': T_2001, '2': T_2023})
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert body['count'] == 1
        assert _ids(body) == ['1']
        assert (body['user_id'], body['sec_uid']) == (GENUINE_UID, GENUINE_SEC)
        # Not merely "the right ids are present": the impostor's must be
        # absent from every byte of the reply. The page is terminal, so
        # `page_token` is null and the whole body is comparable.
        assert body['page_token'] is None
        raw = json.dumps(body)
        assert IMPOSTOR_UID not in raw
        assert IMPOSTOR_SEC not in raw


# ============ 6: the out-of-range `create_time` is served as an unknown date
class TestAnOutOfRangeCreateTimeIsServedAsAnUnknownDate:
    """`_iso_utc` degrading is pinned above as a property of the pure
    function. That pin alone is not enough, though: it stays green if the
    degradation never reaches the client, so what the caller actually GETS is
    pinned here, end to end through the real app.

    This class previously pinned the opposite: `_iso_utc` raised `ValueError`
    on an out-of-range `create_time`, nothing between `flatten_video` and the
    ASGI boundary caught it (`pool.run_call` does not, and `_domain_errors`
    maps only the domain classes), and an absurd upstream value escaped the
    handler as a 500 — on `/search` as much as on `/user/posts`. That is now
    fixed in `mapping._iso_utc`, so the assertions below are re-pinned to the
    fixed behaviour: the record is SERVED, with `create_time: null`, and it
    sorts LAST rather than claiming to be the newest post.

    The failure was never an identity one — `transport.calls` is non-empty, so
    TikTok answered fine — which is why it must not surface as a 502 either."""

    # One second past `datetime.max` in UTC, and an absurd millisecond-looking
    # value — the shape a `create_time` field would carry if upstream ever
    # switched it to milliseconds.
    OUT_OF_RANGE = MAX_TS + 1
    MILLISECONDS = 1_700_000_000_000

    def _page(self, transport, ts: int) -> None:
        _by_path(transport, {
            SEARCH_VIDEO_PATH: reply(['1'], cursor=1, has_more=False,
                                     create_times={'1': ts}),
            SEARCH_ITEM_PATH: reply(['1'], cursor=1, has_more=False,
                                    key='search_item_list',
                                    create_times={'1': ts}),
        })

    @pytest.mark.parametrize('ts', [OUT_OF_RANGE, MIN_TS - 1, MILLISECONDS])
    def test_search_serves_the_record_with_a_null_create_time(
            self, tmp_path, transport, ts):
        self._page(transport, ts)
        status, body = _post(_app(tmp_path), '/search',
                             {'type': 'keyword', 'query': 'ocean',
                              'limit': 10})[0]
        # 200, not 500 and not 502: the request DID reach TikTok and DID come
        # back fine, so no identity is at fault and none should be charged. An
        # unrenderable date costs the date, never the record.
        assert status == 200
        assert _ids(body) == ['1']
        assert body['results'][0]['create_time'] is None
        assert transport.calls, 'nothing was requested, so nothing was mapped'

    def test_user_posts_serves_the_same_record_and_sorts_it_last(
            self, tmp_path, transport):
        # The same value on the other endpoint, and here the ordering
        # consequence is observable: upstream serves the undated record FIRST,
        # and the sentinel key must push it behind every dated one — an
        # undated record must not claim to be the newest post.
        items, times = _authored([('1', self.OUT_OF_RANGE), ('2', T_2001),
                                  ('3', T_2023)])
        _single_page(transport, items, times)
        status, body = _posts(_app(tmp_path), transport)
        assert status == 200
        assert _ids(body) == ['3', '2', '1']
        assert _times(body) == [_iso(T_2023), _iso(T_2001), None]

    @pytest.mark.parametrize('ts, rendered', [
        (MAX_TS, '9999-12-31T23:59:59+00:00'),
        (MIN_TS, '0001-01-01T00:00:00+00:00'),
    ])
    def test_the_boundary_values_themselves_are_served_normally(
            self, tmp_path, transport, ts, rendered):
        # The other side of the bound: `MAX_TS` / `MIN_TS` EXACTLY are IN range
        # and must still come back with their real date, so the tests above pin
        # an off-by-one edge and not "big timestamps degrade". A fix that
        # over-catches — clamping a plausible range, say — turns these two into
        # nulls and fails here.
        self._page(transport, ts)
        status, body = _post(_app(tmp_path), '/search',
                             {'type': 'keyword', 'query': 'ocean', 'limit': 10})[0]
        assert status == 200
        assert body['results'][0]['create_time'] == rendered
