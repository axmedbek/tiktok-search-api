"""Unit tests for capture_diff.py — the param-diff diagnostic.

This module is a TOOL WHOSE OUTPUT DECIDES THE NEXT TASK. `/aweme/v1/aweme/post/`
and `/aweme/v1/user/profile/other/` reject this client's params with a bodyless
HTTP 200 and no feedback; every other explanation was ruled out by measurement,
so the param diff is the one bounded method left. If the tool mislabels the
param class being hunted as "not a gap", the next task builds on a false diff —
which is exactly what two review rounds caught it doing, twice.

So the assertions here are about DIRECTION, not coverage:

* `[replayed]` may only ever be UNDER-claimed. A name one identity carries and
  another does not must read `[SUSPECT]` under the no-selector default, because
  `_common_params` replays ONE identity's `device_query` and the pool picks the
  slot. A union over entries credits identity A with B's key and deletes a real
  suspect from the shortlist.
* The exact replay set must EQUAL the names `client._common_params()` really
  produces. Everything `[replayed]` claims rests on that equality.
* A malformed identity file must yield None, never an empty-but-"exact" set:
  the empty set silences nothing, but it makes the report CLAIM exactness off a
  file that supports no claim, and stops `main` refusing an explicit flag.
* No value from a capture or an identity file reaches stdout unmasked, under
  any of the alias spellings the app sends the same identifier under.

No network, no signer, no live client: the module builds no client at all, and
the one client built here (for the exactness pin) only has `_common_params`
called on it. `mobile/identities.json` does not exist in this repo and is never
read; `mobile/config_direct.yaml` is never read either — every test that
touches config resolution passes an explicit `--config` under `tmp_path`.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import io
import json
import logging
import sys
import urllib.parse
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    FAKE_COOKIE,
    FAKE_TOKEN,
    STUB_SIGNER_KEY,
    identity,
    write_identities,
)
from tiktoksearch import capture_diff  # noqa: E402
from tiktoksearch import client as client_module  # noqa: E402
from tiktoksearch.capture_diff import (  # noqa: E402
    COMMON_PARAM_NAMES,
    EXIT_BAD_IDENTITIES,
    EXIT_NO_CAPTURE,
    EXIT_OK,
    FILTER_PARAM_NAMES,
    IDENTITIES_ENV,
    MARK_ALSO_ON_SEARCH,
    MARK_REPLAYED,
    MARK_SUSPECT,
    MARK_UNDER_FILTERS,
    CallKind,
    ReplaySource,
    SearchPathParams,
    build_replay_set,
    device_query_names,
    expected_params,
    main,
    render_report,
    resolve_identities_path,
    search_path_params,
)
# The capture-side half lives in its own stdlib-only module so the mitmproxy
# addon can import it without the app's dep set (`capture_record.py` docstring).
from tiktoksearch.capture_record import (  # noqa: E402
    MASK_ABSENT,
    MASK_ELLIPSIS,
    MASK_KEEP_HEAD,
    MASK_KEEP_TAIL,
    MASK_MIN_LEN,
    MASK_SHAPE_LEN,
    MASK_SHORT,
    SENSITIVE_PARAM_NAMES,
    CapturedRequest,
    load_capture,
    mask_value,
)
from tiktoksearch.client import (  # noqa: E402
    SEARCH_ITEM_PATH,
    SEARCH_PATHS,
    SEARCH_USER_PATH,
    SEARCH_VIDEO_PATH,
    USER_POSTS_PATH,
    USER_PROFILE_PATH,
)
from tiktoksearch.config import SIGNER_RAPID, ClientConfig  # noqa: E402
from tiktoksearch.filters import PublishTime, SearchFilters, SortType  # noqa: E402

# A path the app uses and this client does not, for the "no *" listing.
FOREIGN_PATH = '/aweme/v1/im/get_conversation/'
FIXTURE_CAPTURE = Path(__file__).resolve().parent / 'fixtures' / 'captured_requests_sample.jsonl'

# The literal filter param names, pinned BESIDE the derived constant. A bound
# asserted only against its own constant moves with it and proves nothing
# (agent_docs/testing.md); this is the half that fails when `to_query_params`
# drifts or when someone replaces the derivation with a stale literal.
EXPECTED_FILTER_NAMES = frozenset((
    'is_filter_search', 'filter_by', 'sort_type',
    'general_filter_sort_type', 'publish_time', 'filter_selected'))

# --- heterogeneous identity file --------------------------------------------
# The ONLY file shape that can tell an intersection apart from a union: each
# entry carries a `device_query` key the other does not. `region`/`cdid` are
# shared, `app_language` is A-only, `carrier_region`/`openudid` are B-only.
DQ_A = {'device_id': 'DEV-A', 'iid': 'IID-DEV-A', 'cdid': 'FAKECDID-A-0001',
        'region': 'AZ', 'app_language': 'en'}
DQ_B = {'device_id': 'DEV-B', 'iid': 'IID-DEV-B', 'cdid': 'FAKECDID-B-0001',
        'region': 'AZ', 'carrier_region': 'AZ', 'openudid': 'FAKEOPENUDID-B-0001'}
SHARED_DQ_NAMES = frozenset(('device_id', 'iid', 'cdid', 'region'))
A_ONLY = 'app_language'
B_ONLY_PLAIN = 'carrier_region'
B_ONLY_SENSITIVE = 'openudid'
# On both the app's search request and its post grid, with a DIFFERENT value on
# each — the request-context class of param that never enters `device_query`
# and is the likeliest thing a bodyless-200 validator wants.
CONTEXT_PARAM = 'enter_from'
CONTEXT_ON_SEARCH = 'search_result'
CONTEXT_ON_POSTS = 'others_homepage'


def _secret(name: str) -> str:
    """A long, recognisable FAKE value for a sensitive param — never a real id.

    Longer than `MASK_MIN_LEN`, so it masks to head+ellipsis+tail rather than
    degrading to `MASK_SHORT`; that is the shape whose leak a substring search
    can actually detect."""
    return f'FAKE{name.upper().replace("_", "")}VALUE-0001'


def _record(path: str, params: dict | None = None, *, method: str = 'GET',
            host: str = 'api16-normal-c-useast1a.tiktokv.com',
            headers: tuple[str, ...] = ('x-argus',)) -> CapturedRequest:
    """One capture record, built through the same boundary validator the loader
    uses — so its params are masked exactly as a real one's are."""
    return CapturedRequest.from_mapping({
        'path': path, 'method': method, 'host': host,
        'params': dict(params or {}), 'headers': list(headers)})


def _het_file(tmp_path, *, entries=('A', 'B')) -> str:
    """A synthetic identity file over the heterogeneous entries above."""
    by_key = {'A': identity('DEV-A', device_query=DQ_A),
              'B': identity('DEV-B', device_query=DQ_B)}
    path = tmp_path / 'ids.json'
    write_identities(path, [by_key[key] for key in entries], stamp=1_700_000_000)
    return str(path)


def _posts_record() -> CapturedRequest:
    """The app's post-grid request, carrying one key from each class: shared,
    A-only, B-only (plain and sensitive), and a request-context param."""
    return _record(USER_POSTS_PATH, {
        'source': '0', 'user_id': '1234567890',
        'sec_user_id': 'MS4wLjABAAAAfake_sec_uid_value', 'max_cursor': '0',
        'count': '20', CONTEXT_PARAM: CONTEXT_ON_POSTS, 'pull_type': '0',
        A_ONLY: 'en', B_ONLY_PLAIN: 'AZ', B_ONLY_SENSITIVE: _secret(B_ONLY_SENSITIVE),
        'cdid': _secret('cdid'), 'region': 'AZ'})


def _search_record() -> CapturedRequest:
    """The app's own video-search request, so `on_search` is populated and the
    cross-path note has something to say."""
    return _record(SEARCH_VIDEO_PATH, {
        'keyword': 'baku esaz', 'count': '10', 'offset': '0',
        'search_source': 'normal_search', CONTEXT_PARAM: CONTEXT_ON_SEARCH,
        'pull_type': '0', B_ONLY_PLAIN: 'AZ', B_ONLY_SENSITIVE: _secret(B_ONLY_SENSITIVE),
        A_ONLY: 'en', 'region': 'AZ'})


def _section(lines: list[str], path: str) -> list[str]:
    """The report lines belonging to the `--- <kind>  <path>` section."""
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith('--- '):
            inside = line.endswith(path)
            continue
        if line.startswith('How to read this:'):
            break
        if inside:
            out.append(line)
    return out


_MARKS = (MARK_SUSPECT, MARK_REPLAYED, MARK_ALSO_ON_SEARCH, MARK_UNDER_FILTERS)


def _rendered(lines: list[str], path: str, name: str) -> str | None:
    """The marker the REPORT actually printed for `name` under `path`, or None
    when the report did not list it at all.

    Reading the rendered line rather than calling `_marker` is deliberate: the
    marker is only worth anything if it reaches the page the operator reads."""
    for line in _section(lines, path):
        stripped = line.strip()
        for mark in _MARKS:
            if stripped.startswith(mark) and stripped[len(mark):].strip().split(' = ')[0] == name:
                return mark
    return None


def _line_for(lines: list[str], path: str, name: str) -> str:
    for line in _section(lines, path):
        stripped = line.strip()
        for mark in _MARKS:
            if stripped.startswith(mark) and stripped[len(mark):].strip().split(' = ')[0] == name:
                return line
    raise AssertionError(f'{name!r} not listed under {path}')


class TestReplaySetSource:
    """Check 1 — where the replay set came from, and what a bad file yields.

    Every degenerate shape must yield None, NOT an empty `frozenset()`. An
    empty set silences nothing by itself, but it is tagged `DEVICE_QUERY_ONE`,
    so the report claims EXACTNESS off a file that supports no claim and `main`
    stops refusing an explicit `--identities`."""

    def test_bare_list_shape_is_accepted(self, tmp_path):
        path = _het_file(tmp_path, entries=('B',))
        got = device_query_names(path)
        assert got is not None
        assert got.names == frozenset(DQ_B)
        assert got.source is ReplaySource.DEVICE_QUERY_ONE
        assert got.entries == 1

    def test_wrapped_shape_is_accepted(self, tmp_path):
        path = tmp_path / 'ids.json'
        path.write_text(json.dumps({'identities': [identity('DEV-B', device_query=DQ_B)]}),
                        encoding='utf-8')
        got = device_query_names(str(path))
        assert got is not None
        assert got.names == frozenset(DQ_B)
        assert got.source is ReplaySource.DEVICE_QUERY_ONE

    @pytest.mark.parametrize('shape', ['absent', 'oserror', 'bad_json', 'not_a_list',
                                       'wrapped_empty', 'no_device_query',
                                       'empty_device_query', 'no_identity_key',
                                       'entries_not_objects'])
    def test_degenerate_file_yields_none_not_an_empty_set(self, tmp_path, shape):
        path = tmp_path / 'ids.json'
        if shape == 'absent':
            pass
        elif shape == 'oserror':
            path.mkdir()  # a directory reads as OSError, not as JSON
        elif shape == 'bad_json':
            path.write_text('{"identities": [', encoding='utf-8')
        elif shape == 'not_a_list':
            path.write_text('42', encoding='utf-8')
        elif shape == 'wrapped_empty':
            path.write_text(json.dumps({'identities': []}), encoding='utf-8')
        elif shape == 'no_device_query':
            path.write_text(json.dumps([{'device_id': 'DEV-A', 'cookie': FAKE_COOKIE}]),
                            encoding='utf-8')
        elif shape == 'empty_device_query':
            path.write_text(json.dumps([{'device_id': 'DEV-A', 'device_query': {}}]),
                            encoding='utf-8')
        elif shape == 'no_identity_key':
            # No `device_id` at top level AND none inside `device_query`, which
            # is the pair `identity_manager._identity_key` reads. An entry with
            # a key only in `device_query` is NOT keyless — see the positive
            # test below, which is why this shape has to strip both.
            path.write_text(json.dumps([{'cookie': FAKE_COOKIE,
                                         'device_query': {'region': 'AZ'}}]),
                            encoding='utf-8')
        else:
            path.write_text(json.dumps(['not-an-object', 7]), encoding='utf-8')

        got = device_query_names(str(path))
        # `is None`, not falsy: an empty frozenset() is falsy too, and it is
        # precisely the wrong answer this asserts against.
        assert got is None, f'{shape} produced a claim the file cannot support: {got!r}'

        replay = build_replay_set(SearchPathParams(values={'seen': 'x'}), str(path))
        assert replay.source is ReplaySource.SEARCH_PATHS
        assert replay.from_device_query is False
        assert replay.is_exact is False

    def test_device_query_source_is_used_when_keys_exist(self, tmp_path):
        replay = build_replay_set(SearchPathParams(values={'seen': 'x'}),
                                  _het_file(tmp_path, entries=('B',)))
        assert replay.source is ReplaySource.DEVICE_QUERY_ONE
        assert replay.from_device_query is True
        assert 'seen' not in replay.names, 'the heuristic leaked into the exact branch'

    def test_the_identity_key_falls_back_to_device_query_exactly_as_the_store_does(self, tmp_path):
        """`identity_manager._identity_key` reads `device_id`, then
        `device_query`'s copy of it. An entry this tool considered keyless but
        the store does not would shift every later `--identity` INDEX by one,
        so the index would address a different identity than the pool's `id<n>`
        slot of the same number."""
        first = {'device_query': {'device_id': 'DEV-IN-QUERY', 'region': 'AZ'}}
        path = tmp_path / 'ids.json'
        path.write_text(json.dumps([first, identity('DEV-B', device_query=DQ_B)]),
                        encoding='utf-8')
        by_device_id = device_query_names(str(path), 'DEV-IN-QUERY')
        assert by_device_id is not None
        assert by_device_id.names == frozenset(('device_id', 'region'))
        # It occupies index 0, so DEV-B is index 1 and not index 0.
        by_index = device_query_names(str(path), '1')
        assert by_index is not None
        assert by_index.names == frozenset(DQ_B)

    def test_a_device_id_match_wins_over_the_index_reading(self, tmp_path):
        """A numeric device_id must never be shadowed by the index reading of
        the same string."""
        path = tmp_path / 'ids.json'
        path.write_text(json.dumps([identity('DEV-A', device_query=DQ_A),
                                    identity('1', device_query=DQ_B)]),
                        encoding='utf-8')
        got = device_query_names(str(path), '1')
        assert got is not None
        # Both readings happen to land on entry 1 here, so pin the direction
        # with a device_id whose index reading points elsewhere.
        path.write_text(json.dumps([identity('1', device_query=DQ_B),
                                    identity('DEV-A', device_query=DQ_A)]),
                        encoding='utf-8')
        got = device_query_names(str(path), '1')
        assert got is not None
        assert got.names == frozenset(DQ_B), 'the index reading shadowed a numeric device_id'

    def test_selector_that_matches_nothing_yields_none(self, tmp_path):
        assert device_query_names(_het_file(tmp_path), 'DEV-NOPE') is None
        assert device_query_names(_het_file(tmp_path), '9') is None


class TestExactnessPin:
    """Check 2 — the anti-drift guard. The exact replay set must EQUAL the name
    set `client._common_params()` really produces for that identity's config.

    Everything `[replayed]` claims rests on this equality: the tool excludes
    those names from the suspect list on the grounds that our own request
    already sends them. If `_common_params` gains or drops a key, this fails."""

    @staticmethod
    def _client(device_query: dict, *, device_id: str, iid: str):
        """A DIRECT-mode client. Its signer class is the autouse `FakeSigner`,
        so nothing is constructed that could sign or send anything; only
        `_common_params` is ever called on it."""
        cfg = ClientConfig(signer=SIGNER_RAPID, rapidapi_key=STUB_SIGNER_KEY,
                           device_id=device_id, iid=iid, device_query=device_query)
        return client_module.TikTokClient(cfg)

    def test_common_param_names_is_exactly_what_common_params_adds(self):
        """With an EMPTY `device_query`, `_common_params` produces exactly the
        five names, so this pins the constant against the code with nothing
        else in the way."""
        client = self._client({}, device_id='DEV-B', iid='IID-DEV-B')
        assert frozenset(client._common_params()) == COMMON_PARAM_NAMES

    def test_exact_replay_set_equals_common_params_for_a_realistic_identity(self, tmp_path):
        client = self._client(dict(DQ_B), device_id='DEV-B', iid='IID-DEV-B')
        produced = frozenset(client._common_params())
        replay = build_replay_set(SearchPathParams(values={}),
                                  _het_file(tmp_path, entries=('B',)))
        assert replay.is_exact
        assert replay.names == produced
        # Non-vacuous: the set is bigger than the five constants and carries
        # the per-request names, so equality is not two empty sets agreeing.
        assert produced > COMMON_PARAM_NAMES
        assert {'ts', '_rticket', 'aid'} <= produced
        assert frozenset(DQ_B) <= produced

    def test_exact_replay_set_equals_common_params_for_a_fingerprint_only_identity(self, tmp_path):
        """A `device_query` carrying NONE of the five: `_common_params`
        `setdefault`s device_id/iid/aid and assigns ts/_rticket itself, so
        dropping any one of them from `COMMON_PARAM_NAMES` breaks equality
        here — which the realistic shape above cannot see, because its own
        `device_query` re-supplies device_id and iid."""
        fingerprint = {'cdid': 'FAKECDID-B-0001', 'region': 'AZ', 'carrier_region': 'AZ'}
        entry = {'device_id': 'DEV-B', 'device_query': fingerprint}
        path = tmp_path / 'ids.json'
        path.write_text(json.dumps([entry]), encoding='utf-8')

        client = self._client(dict(fingerprint), device_id='DEV-B', iid='IID-DEV-B')
        produced = frozenset(client._common_params())
        replay = build_replay_set(SearchPathParams(values={}), str(path))
        assert replay.is_exact
        assert replay.names == produced
        assert COMMON_PARAM_NAMES.isdisjoint(fingerprint), 'the pin lost its discriminating power'


class TestSourceScopingAndDirection:
    """Check 3 — the test that must bite. `[replayed]` may only ever be
    UNDER-claimed.

    A `device_query` key only ONE entry carries is not replayed by whichever
    identity the pool happens to pick, so with no selector it stays in the
    suspect list on purpose. Unioning across entries would credit identity A
    with B's key and print `[replayed]` over a genuine gap — deleting the one
    thing this tool exists to find."""

    def _report(self, tmp_path, *, entries=('A', 'B'), selector=None) -> list[str]:
        path = _het_file(tmp_path, entries=entries)
        return render_report([_search_record(), _posts_record()], path, selector=selector)

    def test_multi_entry_no_selector_intersects(self, tmp_path):
        got = device_query_names(_het_file(tmp_path))
        assert got is not None
        assert got.source is ReplaySource.DEVICE_QUERY_COMMON
        assert got.entries == 2
        assert got.names == SHARED_DQ_NAMES
        assert A_ONLY not in got.names
        assert B_ONLY_PLAIN not in got.names

    @pytest.mark.parametrize('selector', ['DEV-B', '1'])
    def test_selector_uses_that_entry_alone_and_is_exact(self, tmp_path, selector):
        got = device_query_names(_het_file(tmp_path), selector)
        assert got is not None
        assert got.source is ReplaySource.DEVICE_QUERY_ONE
        assert got.entries == 1
        assert got.names == frozenset(DQ_B)

    def test_single_entry_file_is_exact_without_a_selector(self, tmp_path):
        got = device_query_names(_het_file(tmp_path, entries=('B',)))
        assert got is not None
        assert got.source is ReplaySource.DEVICE_QUERY_ONE
        assert got.names == frozenset(DQ_B)

    def test_a_key_only_one_entry_carries_stays_a_suspect_by_default(self, tmp_path):
        """The direction pin. Rendered, not computed."""
        lines = self._report(tmp_path)
        for name in (A_ONLY, B_ONLY_PLAIN, B_ONLY_SENSITIVE):
            assert _rendered(lines, USER_POSTS_PATH, name) == MARK_SUSPECT, name
        for name in ('cdid', 'region'):
            assert _rendered(lines, USER_POSTS_PATH, name) == MARK_REPLAYED, name
        assert 'INTERSECTION' in '\n'.join(lines)

    def test_selecting_the_entry_clears_only_that_entrys_keys(self, tmp_path):
        lines = self._report(tmp_path, selector='DEV-B')
        assert _rendered(lines, USER_POSTS_PATH, B_ONLY_PLAIN) == MARK_REPLAYED
        assert _rendered(lines, USER_POSTS_PATH, B_ONLY_SENSITIVE) == MARK_REPLAYED
        # The other entry's exclusive key is NOT replayed by this identity.
        assert _rendered(lines, USER_POSTS_PATH, A_ONLY) == MARK_SUSPECT
        assert 'EXACT for that identity' in '\n'.join(lines)

    def test_selecting_the_other_entry_flips_the_same_two_names(self, tmp_path):
        lines = self._report(tmp_path, selector='DEV-A')
        assert _rendered(lines, USER_POSTS_PATH, A_ONLY) == MARK_REPLAYED
        assert _rendered(lines, USER_POSTS_PATH, B_ONLY_PLAIN) == MARK_SUSPECT
        assert _rendered(lines, USER_POSTS_PATH, B_ONLY_SENSITIVE) == MARK_SUSPECT

    def test_single_entry_file_clears_its_own_keys(self, tmp_path):
        lines = self._report(tmp_path, entries=('B',))
        assert _rendered(lines, USER_POSTS_PATH, B_ONLY_PLAIN) == MARK_REPLAYED
        assert _rendered(lines, USER_POSTS_PATH, A_ONLY) == MARK_SUSPECT


class TestFallbackLabelling:
    """Check 4 — with no identity file the claim must get WEAKER, not silent.

    A request-context param (`enter_from`, `pull_type`) rides on both the app's
    search request and its post grid without ever entering `device_query`, and
    that is the likeliest class of param a bodyless-200 validator demands. So
    `[also-on-search]` must never read as "not a gap"."""

    def _fallback(self) -> list[str]:
        return render_report([_search_record(), _posts_record()], None)

    def test_cross_path_param_is_also_on_search_in_fallback(self):
        lines = self._fallback()
        assert _rendered(lines, USER_POSTS_PATH, CONTEXT_PARAM) == MARK_ALSO_ON_SEARCH
        joined = '\n'.join(lines)
        assert 'HEURISTIC' in joined
        assert 'no identity file found' in joined

    def test_fallback_legend_disclaims_it_as_evidence(self):
        joined = '\n'.join(self._fallback())
        assert 'NOT evidence' in joined
        assert 'that we send it' in joined

    def test_same_param_is_a_suspect_once_the_file_is_readable(self, tmp_path):
        lines = render_report([_search_record(), _posts_record()],
                             _het_file(tmp_path), selector='DEV-B')
        assert _rendered(lines, USER_POSTS_PATH, CONTEXT_PARAM) == MARK_SUSPECT
        # The context-dependent VALUE is itself evidence, so it is carried.
        note = f'(a search request sent {CONTEXT_ON_SEARCH!r} instead)'
        assert note in _line_for(lines, USER_POSTS_PATH, CONTEXT_PARAM)
        joined = '\n'.join(lines)
        assert 'NOT evidence' not in joined
        assert 'HEURISTIC' not in joined
        assert MARK_ALSO_ON_SEARCH not in joined

    def test_a_search_path_gets_no_cross_path_note_against_itself(self):
        """`on_search` is the union the search paths came from, so restating it
        against a search path would be a note about itself."""
        lines = self._fallback()
        for line in _section(lines, SEARCH_VIDEO_PATH):
            assert 'a search request sent' not in line


class TestAliasMasking:
    """Check 5 — no value from a capture reaches disk or stdout unmasked, under
    ANY of the alias spellings the app sends the same identifier under."""

    ALL_SENSITIVE = tuple(sorted(SENSITIVE_PARAM_NAMES)) + ('cookie', 'sessionid', 'x_tt_token')
    VERBATIM = {'aid': '1233', 'sec_user_id': 'MS4wLjABAAAAfake_sec_uid_value',
                'sec_uid': 'MS4wLjABAAAAfake_sec_uid_2', 'keyword': 'baku esaz',
                'count': '19', CONTEXT_PARAM: CONTEXT_ON_POSTS}

    def _request(self) -> tuple[CapturedRequest, dict[str, str]]:
        params = {name: _secret(name) for name in self.ALL_SENSITIVE}
        params.update(self.VERBATIM)
        # urlencode, never string concatenation (.claude/rules/security.md).
        url = f'https://api16-normal-c-useast1a.tiktokv.com{USER_POSTS_PATH}?' \
              + urllib.parse.urlencode(params)
        rec = CapturedRequest.from_request(url=url, method='get',
                                          host='api16-normal-c-useast1a.tiktokv.com',
                                          header_names=['X-Argus', 'x-argus', 'Cookie'])
        return rec, params

    def test_every_alias_is_masked_on_the_way_into_the_file(self):
        rec, params = self._request()
        line = rec.as_json_line()
        for name in self.ALL_SENSITIVE:
            raw = params[name]
            assert rec.params[name] == mask_value(name, raw)
            assert rec.params[name] != raw, f'{name} passed through unmasked'
            assert raw not in line, f'{name} value leaked into the capture file'
        assert rec.path == USER_POSTS_PATH
        assert rec.method == 'GET'
        assert rec.headers == ('x-argus', 'cookie')

    def test_non_secret_names_stay_verbatim(self):
        rec, _ = self._request()
        line = rec.as_json_line()
        for name, value in self.VERBATIM.items():
            assert rec.params[name] == value, f'{name} was masked and should not be'
            assert value in line

    def test_no_alias_value_reaches_the_report(self, tmp_path):
        rec, params = self._request()
        lines = render_report([rec, _search_record()], _het_file(tmp_path), selector='DEV-B')
        joined = '\n'.join(lines)
        for name in self.ALL_SENSITIVE:
            assert params[name] not in joined, f'{name} value reached stdout'
        # Non-vacuous: the report really did render this record's params.
        assert self.VERBATIM['keyword'] in joined
        assert self.VERBATIM[CONTEXT_PARAM] in joined
        # And the identity file's own credentials are never even read.
        assert FAKE_COOKIE not in joined
        assert FAKE_TOKEN not in joined

    def test_mask_is_idempotent(self):
        for name in self.ALL_SENSITIVE:
            once = mask_value(name, _secret(name))
            assert mask_value(name, once) == once
            assert len(once) == MASK_SHAPE_LEN

    def test_short_values_degrade_to_the_short_mask(self):
        assert mask_value('device_id', 'A' * MASK_MIN_LEN) == MASK_SHORT
        # Exactly mask-LENGTH but with no ellipsis at the head offset: the
        # length alone must not be read as "already masked", or an 11-char raw
        # identifier passes straight through.
        assert mask_value('device_id', 'A' * MASK_SHAPE_LEN) == MASK_SHORT
        assert mask_value('device_id', 'A' * (MASK_MIN_LEN + 1)) != MASK_SHORT
        assert mask_value('device_id', '') == MASK_ABSENT
        assert mask_value('device_id', None) == MASK_ABSENT
        assert mask_value('keyword', None) == MASK_ABSENT

    def test_an_embedded_ellipsis_does_not_bypass_the_mask(self):
        """`parse_qsl` percent-DECODES, so an upstream `%E2%80%A6` arrives as a
        literal ellipsis. A containment test would wave the whole raw
        identifier through — masking bypassed by an attacker-chosen substring,
        on the one path whose entire job is to prevent that."""
        raw = f'ABCDEF{MASK_ELLIPSIS}0123456789'
        assert MASK_ELLIPSIS in raw
        assert len(raw) != MASK_SHAPE_LEN
        masked = mask_value('device_id', raw)
        assert masked != raw
        assert masked == raw[:MASK_KEEP_HEAD] + MASK_ELLIPSIS + raw[-MASK_KEEP_TAIL:]

    def test_an_ellipsis_bypass_is_also_blocked_through_the_url_boundary(self):
        raw = f'ABCDEF{MASK_ELLIPSIS}0123456789'
        url = (f'https://host{USER_POSTS_PATH}?'
               + urllib.parse.urlencode({'device_id': raw}))
        rec = CapturedRequest.from_request(url=url, method='GET', host='host',
                                           header_names=[])
        assert MASK_ELLIPSIS.encode('utf-8').hex() == 'e280a6'  # it really was %E2%80%A6
        assert rec.params['device_id'] != raw
        assert raw not in rec.as_json_line()

    def test_a_genuinely_pre_masked_value_passes_through_unchanged(self):
        """`681234…4567` is 11 chars, which trips the short-value rule and
        would otherwise degrade to `***`, throwing away the recognisability the
        shape exists for. Masking runs on the way in AND on the way out."""
        pre_masked = f'681234{MASK_ELLIPSIS}4567'
        assert len(pre_masked) == MASK_SHAPE_LEN <= MASK_MIN_LEN
        assert mask_value('device_id', pre_masked) == pre_masked

    def test_a_mask_of_a_value_that_itself_held_an_ellipsis_still_round_trips(self):
        """A genuine mask output can carry a further ellipsis in its head or
        tail; rejecting that would reintroduce the `***` degradation."""
        raw = f'abc{MASK_ELLIPSIS}de-middle-part-7890'
        once = mask_value('device_id', raw)
        assert MASK_ELLIPSIS in once[:MASK_KEEP_HEAD]
        assert mask_value('device_id', once) == once


class TestUnderFilters:
    """Check 6 — the filtered-search excuse, and the constant that justifies
    deriving `FILTER_PARAM_NAMES` instead of listing it."""

    def _filter_record(self, path: str) -> CapturedRequest:
        params = SearchFilters(sort_type=SortType.MOST_LIKED,
                               publish_time=PublishTime.LAST_WEEK).to_query_params()
        return _record(path, {'keyword': 'baku esaz', 'count': '10', 'offset': '0',
                              'search_source': 'normal_search', **params})

    @pytest.mark.parametrize('path', [SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH])
    def test_filter_names_are_excused_on_both_video_search_paths(self, tmp_path, path):
        lines = render_report([self._filter_record(SEARCH_VIDEO_PATH),
                               self._filter_record(SEARCH_ITEM_PATH)],
                              _het_file(tmp_path), selector='DEV-B')
        for name in FILTER_PARAM_NAMES:
            assert _rendered(lines, path, name) == MARK_UNDER_FILTERS, name

    def test_filter_names_are_not_excused_on_the_posts_path(self, tmp_path):
        filters = SearchFilters(sort_type=SortType.MOST_LIKED,
                                publish_time=PublishTime.LAST_WEEK).to_query_params()
        posts = _record(USER_POSTS_PATH, {'source': '0', 'user_id': '1', **filters})
        lines = render_report([posts], _het_file(tmp_path), selector='DEV-B')
        for name in FILTER_PARAM_NAMES:
            assert _rendered(lines, USER_POSTS_PATH, name) == MARK_SUSPECT, name
        assert MARK_UNDER_FILTERS not in '\n'.join(_section(lines, USER_POSTS_PATH))

    def test_filter_param_names_matches_every_sort_and_publish_combination(self):
        """The drift guard. Every reachable combination's keys must be covered,
        and their union must be exactly the constant — which is what makes
        deriving it from ONE combination legitimate."""
        union: frozenset[str] = frozenset()
        combos = 0
        for sort_type in (None, *SortType):
            for publish_time in (None, *PublishTime):
                keys = frozenset(SearchFilters(sort_type=sort_type,
                                               publish_time=publish_time).to_query_params())
                assert keys <= FILTER_PARAM_NAMES, (sort_type, publish_time)
                union |= keys
                combos += 1
        assert combos == (len(SortType) + 1) * (len(PublishTime) + 1)
        assert union == FILTER_PARAM_NAMES

    def test_filter_param_names_matches_the_pinned_literal(self):
        """Pinned beside the derivation, so a stale hand-written copy fails
        even when it is internally consistent."""
        assert FILTER_PARAM_NAMES == EXPECTED_FILTER_NAMES

    def test_the_video_search_expectation_really_is_the_unfiltered_one(self):
        """`[under-filters]` is only honest because `expected_params` builds the
        UNFILTERED param set."""
        assert FILTER_PARAM_NAMES.isdisjoint(expected_params(CallKind.VIDEO_SEARCH).params)


class TestResolveIdentitiesPath:
    """Check 7 — precedence. `config_direct.yaml` is never read: every case
    passes an explicit config under `tmp_path`."""

    def test_explicit_wins_over_everything(self, tmp_path, monkeypatch):
        monkeypatch.setenv(IDENTITIES_ENV, str(tmp_path / 'from-env.json'))
        config = tmp_path / 'cfg.yaml'
        config.write_text('identities_path: from-config.json\n', encoding='utf-8')
        assert resolve_identities_path('explicit.json', str(config)) == 'explicit.json'

    def test_explicit_is_returned_unchecked(self, tmp_path, monkeypatch):
        """It RESOLVES, it does not validate — `main` is the path that refuses."""
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        missing = str(tmp_path / 'nope.json')
        assert resolve_identities_path(missing, str(tmp_path / 'cfg.yaml')) == missing

    def test_env_wins_over_config(self, tmp_path, monkeypatch):
        env_path = str(tmp_path / 'from-env.json')
        monkeypatch.setenv(IDENTITIES_ENV, env_path)
        config = tmp_path / 'cfg.yaml'
        config.write_text('identities_path: from-config.json\n', encoding='utf-8')
        assert resolve_identities_path(None, str(config)) == env_path

    def test_relative_config_value_resolves_against_the_configs_dir(self, tmp_path, monkeypatch):
        """The recorded failure this mirrors: a relative path resolved against
        the process cwd made the server fall back to synthetic devices."""
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        nested = tmp_path / 'profiles'
        nested.mkdir()
        config = nested / 'cfg.yaml'
        config.write_text('identities_path: ids.json\n', encoding='utf-8')
        monkeypatch.chdir(tmp_path)
        got = resolve_identities_path(None, str(config))
        assert got == str(nested / 'ids.json')
        assert got != str(tmp_path / 'ids.json'), 'resolved against the cwd, not the config'

    def test_absolute_config_value_passes_through(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        absolute = str(tmp_path / 'elsewhere' / 'ids.json')
        config = tmp_path / 'cfg.yaml'
        config.write_text(f'identities_path: {absolute}\n', encoding='utf-8')
        assert resolve_identities_path(None, str(config)) == absolute

    def test_default_beside_the_config_when_it_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        config = tmp_path / 'cfg.yaml'
        config.write_text('retries: 2\n', encoding='utf-8')
        beside = tmp_path / 'identities.json'
        beside.write_text('[]', encoding='utf-8')
        assert resolve_identities_path(None, str(config)) == str(beside)

    def test_none_when_nothing_resolves(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        config = tmp_path / 'cfg.yaml'
        config.write_text('retries: 2\n', encoding='utf-8')
        assert resolve_identities_path(None, str(config)) is None

    def test_none_when_the_config_itself_is_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        assert resolve_identities_path(None, str(tmp_path / 'no-such.yaml')) is None

    def test_unreadable_config_falls_through_without_raising(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        config = tmp_path / 'cfg.yaml'
        config.write_text('identities_path: [unclosed\n', encoding='utf-8')
        with caplog.at_level(logging.WARNING, logger='tiktoksearch.capture_diff'):
            assert resolve_identities_path(None, str(config)) is None
        assert any('identities_path' in rec.getMessage() for rec in caplog.records)


class TestMainExitCodes:
    """Check 7, second half — a stated flag that cannot be honoured must FAIL.

    A report whose markers are heuristic reads exactly like one whose markers
    were vetted, so degrading an explicit flag answers a different question
    than the one asked."""

    def _run(self, argv) -> tuple[int, str]:
        out = io.StringIO()
        return main(argv, stream=out), out.getvalue()

    def test_bad_identities_flag_refuses_the_heuristic(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        code, text = self._run([str(FIXTURE_CAPTURE),
                                '--identities', str(tmp_path / 'nope.json'),
                                '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_BAD_IDENTITIES == 3
        assert text == '', 'a refused run must not print a report'

    def test_bad_identity_selector_refuses_too(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        code, text = self._run([str(FIXTURE_CAPTURE),
                                '--identities', _het_file(tmp_path),
                                '--identity', 'DEV-NOPE',
                                '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_BAD_IDENTITIES
        assert text == ''

    def test_omitting_both_flags_accepts_the_heuristic(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        code, text = self._run([str(FIXTURE_CAPTURE), '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_OK == 0
        assert 'HEURISTIC' in text

    def test_a_good_selector_renders_the_exact_report(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        code, text = self._run([str(FIXTURE_CAPTURE),
                                '--identities', _het_file(tmp_path),
                                '--identity', 'DEV-B',
                                '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_OK
        assert 'EXACT for that identity' in text
        assert FAKE_COOKIE not in text and FAKE_TOKEN not in text

    def test_missing_capture_exits_no_capture(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        code, text = self._run([str(tmp_path / 'nope.jsonl'),
                                '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_NO_CAPTURE == 2
        assert text == ''

    def test_empty_capture_exits_no_capture(self, tmp_path, monkeypatch):
        monkeypatch.delenv(IDENTITIES_ENV, raising=False)
        empty = tmp_path / 'empty.jsonl'
        empty.write_text('\n\n', encoding='utf-8')
        code, _ = self._run([str(empty), '--config', str(tmp_path / 'cfg.yaml')])
        assert code == EXIT_NO_CAPTURE

    def test_the_committed_fixture_is_loadable_and_committable(self):
        """The fixture lives under `tests/`, which `.gitignore` re-includes with
        `!**/tests/**/*.jsonl` — a repo-wide `*.jsonl` used to swallow it, which
        passes locally off an untracked file and breaks on a fresh clone."""
        records = load_capture(str(FIXTURE_CAPTURE))
        assert len(records) == 8
        assert {r.path for r in records} >= {SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH,
                                             SEARCH_USER_PATH, USER_POSTS_PATH,
                                             USER_PROFILE_PATH, FOREIGN_PATH}
        # Already masked on the way in, and masked again on the way out.
        assert records[0].params['device_id'] == f'FAKEDE{MASK_ELLIPSIS}0001'


class TestLoadCapture:
    """Check 8 — a capture session is expensive to repeat, so one bad line must
    not throw the rest away. The log names the line NUMBER, never its content."""

    SECRET_IN_A_BAD_LINE = 'FAKELEAKDEVICE-0001'

    def _file(self, tmp_path):
        good_a = {'path': SEARCH_VIDEO_PATH, 'method': 'GET', 'host': 'h',
                  'params': {'keyword': 'a'}, 'headers': ['x-argus']}
        good_b = {'path': USER_POSTS_PATH, 'method': 'GET', 'host': 'h',
                  'params': {'source': '0'}, 'headers': ['x-argus']}
        path = tmp_path / 'capture.jsonl'
        path.write_text('\n'.join([
            json.dumps(good_a),
            '',
            '   ',
            # parses, but is a half record: no `path`
            json.dumps({'params': {'device_id': self.SECRET_IN_A_BAD_LINE}}),
            # not an object at all
            json.dumps([1, 2, 3]),
            json.dumps(good_b),
            # truncated mid-write, which is the ordinary case: the addon appends live
            '{"path": "/aweme/v1/aweme/post/", "params": {"device_id": "'
            + self.SECRET_IN_A_BAD_LINE,
        ]) + '\n', encoding='utf-8')
        return path

    def test_blank_lines_are_skipped_and_good_records_survive(self, tmp_path):
        records = load_capture(str(self._file(tmp_path)))
        assert [r.path for r in records] == [SEARCH_VIDEO_PATH, USER_POSTS_PATH]

    def test_each_bad_line_is_logged_by_number_only(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger='tiktoksearch.capture_record'):
            load_capture(str(self._file(tmp_path)))
        messages = [rec.getMessage() for rec in caplog.records]
        assert len(messages) == 3, messages
        for lineno in (4, 5, 7):
            assert any(f'capture line {lineno}:' in msg for msg in messages), lineno
        # The whole point of naming the number: the CONTENT never reaches a log.
        for msg in messages:
            assert self.SECRET_IN_A_BAD_LINE not in msg
            assert 'device_id' not in msg

    def test_a_wholly_unusable_file_returns_an_empty_list(self, tmp_path):
        path = tmp_path / 'junk.jsonl'
        path.write_text('nonsense\n{\n', encoding='utf-8')
        assert load_capture(str(path)) == []


class TestReportShapeAndApiGuards:
    """Check 9 — the report's distinct answers, and the API guards that keep a
    misuse from silently returning an empty diff."""

    def test_never_observed_and_no_params_are_distinct_answers(self, tmp_path):
        """Opposite answers to the question being hunted: one says the capture
        session missed the path, the other says the app called it and sent no
        query params at all — itself a finding."""
        lines = render_report([_record(USER_POSTS_PATH, {})], _het_file(tmp_path),
                              selector='DEV-B')
        posts = '\n'.join(_section(lines, USER_POSTS_PATH))
        profile = '\n'.join(_section(lines, USER_PROFILE_PATH))
        assert 'NOT ONE carried a query' in posts
        assert 'not observed' not in posts
        assert 'not observed' in profile
        assert 'NOT ONE carried a query' not in profile

    def test_paths_the_client_calls_but_the_capture_never_saw_are_listed(self, tmp_path):
        lines = render_report([_record(USER_POSTS_PATH, {'source': '0'})],
                              _het_file(tmp_path), selector='DEV-B')
        joined = '\n'.join(lines)
        assert 'paths this client calls that this capture never saw:' in joined
        assert USER_PROFILE_PATH in joined

    def test_a_foreign_path_is_listed_without_a_star(self):
        lines = render_report(load_capture(str(FIXTURE_CAPTURE)), None)
        observed = [line for line in lines if line.startswith('  ') and 'aweme/v1' in line]
        starred = {line.split()[-1] for line in observed if '*' in line}
        assert USER_POSTS_PATH in starred
        assert FOREIGN_PATH not in starred
        assert any(FOREIGN_PATH in line for line in observed)

    def test_expected_params_raises_on_an_unhandled_kind(self, monkeypatch):
        """Falling through to a default branch would diff a fifth call kind
        against the post-grid param set and report the difference as a
        finding."""
        import enum

        class ExtendedKind(str, enum.Enum):
            VIDEO_SEARCH = 'video_search'
            USER_SEARCH = 'user_search'
            PROFILE = 'profile'
            POSTS = 'posts'
            HASHTAG_SEARCH = 'hashtag_search'

        monkeypatch.setattr(capture_diff, 'CallKind', ExtendedKind)
        with pytest.raises(ValueError, match='no expected param set'):
            capture_diff.expected_params('hashtag_search')

    def test_expected_params_rejects_an_unknown_name(self):
        with pytest.raises(ValueError):
            expected_params('not_a_call_kind')

    def test_every_declared_kind_has_a_param_set(self):
        for kind in CallKind:
            call = expected_params(kind)
            assert call.kind is kind
            assert call.paths
            assert call.params, kind

    @pytest.mark.parametrize('wrap', [list, tuple, set, frozenset])
    def test_search_path_params_accepts_any_collection(self, wrap):
        """The annotation was narrowed to `Collection` after a one-shot
        iterable silently produced an empty replay set."""
        records = [_search_record(), _posts_record()]
        reference = search_path_params(records, SEARCH_PATHS)
        got = search_path_params(records, wrap(SEARCH_PATHS))
        assert got.values == reference.values
        assert got.names == reference.names
        # Non-vacuous: it really found the search-path params.
        assert CONTEXT_PARAM in got.names
        assert got.values[CONTEXT_PARAM] == CONTEXT_ON_SEARCH
        assert 'source' not in got.names, 'a post-grid param leaked into on_search'

    def test_common_param_names_are_in_the_replay_set_in_both_branches(self, tmp_path):
        """`_common_params` sends these five with or without a readable identity
        file, so leaving them out of the fallback rendered five names we
        provably DO send as suspects — enough false leads to bury the real
        one."""
        on_search = SearchPathParams(values={'keyword': 'a'})
        fallback = build_replay_set(on_search, None)
        exact = build_replay_set(on_search, _het_file(tmp_path), 'DEV-B')
        assert fallback.source is ReplaySource.SEARCH_PATHS
        assert exact.source is ReplaySource.DEVICE_QUERY_ONE
        assert COMMON_PARAM_NAMES <= fallback.names
        assert COMMON_PARAM_NAMES <= exact.names

    @pytest.mark.parametrize('with_file', [False, True])
    def test_common_param_names_render_replayed_in_both_branches(self, tmp_path, with_file):
        posts = _record(USER_POSTS_PATH, {
            'source': '0', 'ts': '1700000000', '_rticket': '1700000000000',
            'aid': '1233', 'device_id': _secret('device_id'), 'iid': _secret('iid')})
        path = _het_file(tmp_path, entries=('B',)) if with_file else None
        lines = render_report([posts], path)
        for name in COMMON_PARAM_NAMES:
            assert _rendered(lines, USER_POSTS_PATH, name) == MARK_REPLAYED, (name, with_file)

    def test_the_replay_header_states_the_scope_it_actually_has(self, tmp_path):
        records = [_search_record(), _posts_record()]
        assert 'EXACT for that identity' in '\n'.join(
            render_report(records, _het_file(tmp_path), selector='DEV-B'))
        assert 'INTERSECTION' in '\n'.join(render_report(records, _het_file(tmp_path)))
        assert 'HEURISTIC' in '\n'.join(render_report(records, None))
