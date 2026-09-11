"""Unit tests for `harvest_spool.py` — the file format the app's feed arrives in.

Nothing here runs mitmproxy, Waydroid or a network call. The module under test
is pure file I/O over `tmp_path`, and that is the point: the mitmproxy addon is
three lines of hook over this file, so everything that could be wrong about the
harvest is testable here.

Two properties get the most weight, because both have a mode where a test looks
sound and proves nothing:

* **The stdlib-only rule.** It is enforced by PARSING THE IMPORTS, not by
  importing the module — importing it under the venv proves nothing at all
  about the interpreter `mitmdump` runs on, which is where
  `capture_requests_addon.py` once failed with `No module named 'gmssl'`, came
  up healthy and recorded an entire session of nothing.
* **The unreadable-vs-empty split.** `status: ok` with an EMPTY `aweme_list` is
  a legitimate feed; a 0-byte body, an undecodable body, a non-JSON body and an
  object with no `aweme_list` key are all responses we could not read. A test
  that only checked "an entry was written" passes for both, and the driver's
  whole timeout/transient contract rests on telling them apart.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch import harvest_spool as spool_module  # noqa: E402
from tiktoksearch.harvest_spool import (  # noqa: E402
    ENTRY_SUFFIX,
    MAX_KEY_CHARS,
    NANOS_PER_SECOND,
    POST_FEED_PATH,
    REASON_EMPTY,
    REASON_NO_AWEME_LIST,
    REASON_NOT_JSON,
    REASON_NOT_OBJECT,
    REASON_UNDECODABLE,
    STATUS_OK,
    STATUS_UNREADABLE,
    TEMP_SUFFIX,
    FileSpool,
    SpoolEntry,
    default_spool_dir,
    entry_name,
    file_key,
    nanos_from_name,
    user_id_from_url,
    write_entry,
)

USER_ID = '7195575867517944837'
OTHER_USER_ID = '7195575867517944838'
FEED_URL = f'https://api32-core-alisg.tiktokv.com{POST_FEED_PATH}?user_id={USER_ID}&max_cursor=0&count=20'
# The three modules the mitmproxy addon is allowed to import on top of the
# standard library. `mitmproxy` is the host; the other two are the stdlib-only
# top-level modules in the package directory.
ADDON_ALLOWED_ROOTS = frozenset({'mitmproxy', 'capture_record', 'harvest_spool'})


def aweme(aweme_id: str, *, create_time: int = 1_700_000_000) -> dict:
    """One raw upstream aweme, the shape `mapping.flatten_video` consumes."""
    return {'aweme_id': aweme_id, 'desc': f'post {aweme_id}', 'create_time': create_time,
            'author': {'uid': USER_ID, 'unique_id': 'sirabasc'}, 'statistics': {}}


def feed_body(ids, *, has_more=True, max_cursor=1_756_628_308_000) -> bytes:
    return json.dumps({'status_code': 0, 'has_more': has_more, 'max_cursor': max_cursor,
                       'aweme_list': [aweme(str(i)) for i in ids]}).encode('utf-8')


def ok_entry(user_id=USER_ID, *, captured_at: float, ids=(1, 2)) -> SpoolEntry:
    return SpoolEntry.from_response(user_id=user_id, captured_at=captured_at, body=feed_body(ids))


def import_roots(path: Path) -> set[str]:
    """Every top-level module name `path` imports at module level."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.add(f'.{node.module or ""}')
            elif node.module:
                roots.add(node.module.split('.')[0])
    return roots


class TestTheStdlibOnlyRule:
    """The addon runs under `mitmdump`, on an interpreter that has none of this
    project's dependencies.

    Asserted by PARSING the import statements. Importing the module here would
    prove only that the venv can import it — and the venv has `gmssl`,
    `pydantic` and `requests`, which is exactly the interpreter the rule is not
    about."""

    def test_harvest_spool_imports_only_the_standard_library(self):
        roots = import_roots(Path(spool_module.__file__))
        assert roots, 'the parse found no imports at all — the test is broken, not the module'
        non_stdlib = {root for root in roots if root != '__future__' and root not in sys.stdlib_module_names}
        assert non_stdlib == set(), f'not importable under mitmdump: {sorted(non_stdlib)}'

    def test_harvest_spool_makes_no_relative_import(self):
        # A relative import cannot resolve when the file is loaded as a
        # TOP-LEVEL module, which is how the addon loads it.
        assert {root for root in import_roots(Path(spool_module.__file__)) if root.startswith('.')} == set()

    def test_the_addon_imports_only_mitmproxy_the_stdlib_and_the_two_shared_modules(self):
        addon = Path(spool_module.__file__).resolve().parents[1] / 'harvest_spool_addon.py'
        roots = import_roots(addon)
        assert 'harvest_spool' in roots, 'the addon must reach the spool as a top-level module'
        extra = {root for root in roots
                 if root != '__future__' and root not in sys.stdlib_module_names
                 and root not in ADDON_ALLOWED_ROOTS}
        assert extra == set(), f'the addon would fail to load under mitmdump because of {sorted(extra)}'

    def test_the_addon_never_imports_the_package(self):
        addon = Path(spool_module.__file__).resolve().parents[1] / 'harvest_spool_addon.py'
        # `tiktoksearch/__init__.py` imports `.client` -> `.signing` ->
        # `gmssl`. Reaching the package by name at all reintroduces the exact
        # failure this split exists to prevent.
        assert 'tiktoksearch' not in import_roots(addon)


class TestTheDefaultSpoolDirectory:
    """Resolved from the module file and NOT from the process cwd — the lesson
    `.claude/rules/learned-lessons.md` records about `identities_path`, where a
    cwd-relative path made the server silently miss its identities."""

    def test_it_is_absolute_and_sits_beside_the_package(self):
        resolved = Path(default_spool_dir())
        assert resolved.is_absolute()
        assert (resolved.parent / 'worker.py').is_file(), 'the default belongs under mobile/'

    def test_it_does_not_depend_on_the_working_directory(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        assert Path(default_spool_dir()).is_absolute()
        assert Path(default_spool_dir()).parent != tmp_path


class TestUserIdFromUrl:
    def test_it_reads_the_user_id_param(self):
        assert user_id_from_url(FEED_URL) == USER_ID

    def test_a_url_with_no_user_id_yields_the_empty_string(self):
        assert user_id_from_url(f'https://host{POST_FEED_PATH}?max_cursor=0') == ''

    def test_the_first_value_wins_on_a_repeated_name(self):
        url = f'https://host{POST_FEED_PATH}?user_id={USER_ID}&user_id={OTHER_USER_ID}'
        assert user_id_from_url(url) == USER_ID

    def test_a_blank_value_is_not_a_user_id(self):
        assert user_id_from_url(f'https://host{POST_FEED_PATH}?user_id=&count=20') == ''


class TestFileKey:
    def test_a_real_uid_passes_through_unchanged(self):
        assert file_key(USER_ID) == USER_ID

    def test_a_traversal_attempt_cannot_escape_the_directory(self):
        key = file_key('../../etc/passwd')
        assert '/' not in key and '.' not in key
        assert not key.startswith('%2E%2E%2F'.lower())

    def test_a_separator_bearing_value_cannot_shadow_another_key(self):
        assert os.sep not in file_key(f'{USER_ID}{os.sep}evil')

    def test_the_name_separator_can_never_appear_inside_a_key(self):
        # What makes `<key>-<stamp>.json` an unambiguous grammar: a key
        # carrying a `-` would make the census prefix and the stamp parse
        # depend on each other.
        assert '-' not in file_key('7195-575867-517944837')

    def test_a_key_cannot_become_a_dot_file(self):
        assert not file_key('.hidden').startswith('.')

    def test_it_is_bounded(self):
        assert len(file_key('9' * 500)) == MAX_KEY_CHARS

    def test_nothing_usable_yields_the_empty_string(self):
        assert file_key('   ') == ''


class TestEntryNames:
    def test_a_name_round_trips_its_nanosecond_stamp(self):
        captured_at = 1_789_012_345.5
        name = entry_name(USER_ID, captured_at)
        assert name.startswith(f'{USER_ID}-') and name.endswith(ENTRY_SUFFIX)
        assert nanos_from_name(name) == int(captured_at * NANOS_PER_SECOND)

    def test_an_unparseable_name_yields_none_so_the_reader_falls_open(self):
        # None must mean "read this file", never "skip it": the name is a
        # prefilter and may only make the scan cheaper.
        assert nanos_from_name('renamed-by-a-human.json') is None
        assert nanos_from_name(f'{USER_ID}-123.txt') is None
        assert nanos_from_name('no-separator.json') is None


class TestClassification:
    """`ok` vs `unreadable`, which is the split the driver's whole
    timeout-versus-result contract rests on."""

    def test_a_feed_is_ok_and_carries_its_pagination_state(self):
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=feed_body([1, 2, 3]))
        assert entry.status == STATUS_OK and entry.is_ok
        assert entry.reason is None
        assert len(entry.aweme_list) == 3
        assert entry.has_more is True
        assert entry.max_cursor == 1_756_628_308_000
        assert entry.status_code == 0
        assert entry.path == POST_FEED_PATH

    def test_an_empty_aweme_list_is_a_legitimate_ok_result(self):
        # An account with no posts, or the end of a feed. NOT an error, and
        # this is the case the driver returns rather than raising.
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0,
                                         body=feed_body([], has_more=False))
        assert entry.is_ok
        assert entry.aweme_list == ()
        assert entry.has_more is False

    def test_a_zero_byte_body_is_unreadable_and_not_an_empty_feed(self):
        # HTTP 200 with 0 bytes and `tt_orcas_res: 1` is the measured
        # rejection shape. Reporting it as zero posts is what
        # `.claude/rules/anti-block.md` forbids.
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=b'')
        assert entry.status == STATUS_UNREADABLE and not entry.is_ok
        assert entry.reason == REASON_EMPTY
        assert entry.byte_length == 0

    def test_an_undecodable_body_is_unreadable_with_its_own_reason(self):
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=None)
        assert entry.reason == REASON_UNDECODABLE

    def test_a_non_json_body_is_unreadable(self):
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=b'<html>nginx</html>')
        assert entry.reason == REASON_NOT_JSON
        assert entry.byte_length == len(b'<html>nginx</html>')

    def test_json_that_is_not_an_object_is_unreadable(self):
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=b'[1, 2, 3]')
        assert entry.reason == REASON_NOT_OBJECT

    def test_an_object_with_no_aweme_list_key_is_unreadable(self):
        # Key PRESENCE, never truthiness — the same rule `POSTS_PAYLOAD` in
        # `client.py` applies. An absent key is a reply we did not understand.
        body = json.dumps({'status_code': 0, 'has_more': 0}).encode('utf-8')
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=body)
        assert entry.reason == REASON_NO_AWEME_LIST

    def test_no_part_of_an_unreadable_body_is_kept(self):
        secret_looking = b'sessionid=NOT-A-REAL-COOKIE; x-tt-token=NOT-A-REAL-TOKEN'
        entry = SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=secret_looking)
        rendered = entry.as_json()
        assert 'NOT-A-REAL-COOKIE' not in rendered and 'NOT-A-REAL-TOKEN' not in rendered
        assert entry.byte_length == len(secret_looking)


class TestFlagCoercion:
    """TikTok serves `has_more` as numeric 0/1 and a live reply may render it
    as the STRINGS '0'/'1' — and bare `bool('0')` is True, which would report
    an exhausted feed as having more pages."""

    @pytest.mark.parametrize('raw,expected', [(1, True), (0, False), ('1', True), ('0', False),
                                              (True, True), (False, False), (None, False)])
    def test_has_more_compares_the_number_rather_than_its_truthiness(self, raw, expected):
        body = json.dumps({'has_more': raw, 'aweme_list': []}).encode('utf-8')
        assert SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=body).has_more is expected

    @pytest.mark.parametrize('raw,expected', [(17, 17), ('17', 17), (None, None), ('', None), ('x', None)])
    def test_max_cursor_is_coerced_or_none(self, raw, expected):
        body = json.dumps({'max_cursor': raw, 'aweme_list': []}).encode('utf-8')
        assert SpoolEntry.from_response(user_id=USER_ID, captured_at=1.0, body=body).max_cursor == expected


class TestTheEntryFileOnDisk:
    def test_an_entry_round_trips_through_the_spool(self, tmp_path):
        entry = ok_entry(captured_at=1_789_012_345.5, ids=[1, 2, 3])
        written = write_entry(str(tmp_path), entry)
        back = SpoolEntry.from_mapping(json.loads(Path(written).read_text(encoding='utf-8')))
        assert back.user_id == USER_ID
        assert back.captured_at == entry.captured_at
        assert len(back.aweme_list) == 3
        assert back.has_more == entry.has_more and back.max_cursor == entry.max_cursor

    def test_the_write_leaves_no_temp_file_behind(self, tmp_path):
        write_entry(str(tmp_path), ok_entry(captured_at=1.0))
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(TEMP_SUFFIX)] == []

    def test_a_temp_file_is_never_visible_to_the_reader(self, tmp_path):
        # A half-written file must never be readable
        # (`.claude/rules/learned-lessons.md` on `identities.json`). The temp
        # suffix is what makes that structural rather than a timing hope.
        write_entry(str(tmp_path), ok_entry(captured_at=1.0))
        (tmp_path / f'{USER_ID}-999{TEMP_SUFFIX}').write_text('{half', encoding='utf-8')
        assert len(FileSpool(str(tmp_path)).entry_names(USER_ID)) == 1

    def test_the_directory_is_created_on_demand(self, tmp_path):
        target = tmp_path / 'not' / 'there' / 'yet'
        write_entry(str(target), ok_entry(captured_at=1.0))
        assert len(list(target.iterdir())) == 1

    def test_two_entries_stamped_identically_are_both_kept(self, tmp_path):
        # Losing a captured response silently is the one outcome this file
        # exists to prevent, so a taken name is nudged rather than overwritten.
        write_entry(str(tmp_path), ok_entry(captured_at=5.0, ids=[1]))
        write_entry(str(tmp_path), ok_entry(captured_at=5.0, ids=[2]))
        assert len(FileSpool(str(tmp_path)).entry_names(USER_ID)) == 2

    def test_an_entry_with_no_usable_user_id_is_refused(self, tmp_path):
        entry = SpoolEntry.from_response(user_id='   ', captured_at=1.0, body=feed_body([1]))
        with pytest.raises(ValueError):
            write_entry(str(tmp_path), entry)

    def test_the_entry_is_frozen(self, tmp_path):
        with pytest.raises(Exception):
            ok_entry(captured_at=1.0).user_id = 'mutated'


class TestFromMapping:
    """A spool file is written by another process, which makes it external data
    however friendly that process is."""

    def test_a_non_object_is_refused(self):
        with pytest.raises(ValueError):
            SpoolEntry.from_mapping([1, 2, 3])

    @pytest.mark.parametrize('drop', ['user_id', 'captured_at', 'status'])
    def test_a_missing_required_field_is_refused(self, drop):
        data = json.loads(ok_entry(captured_at=1.0).as_json())
        data.pop(drop)
        with pytest.raises(ValueError):
            SpoolEntry.from_mapping(data)

    def test_an_unknown_status_is_refused(self):
        data = json.loads(ok_entry(captured_at=1.0).as_json())
        data['status'] = 'probably_fine'
        with pytest.raises(ValueError):
            SpoolEntry.from_mapping(data)

    def test_an_ok_entry_with_no_list_is_a_contradiction_not_an_empty_feed(self):
        # This is what keeps "empty means empty" true for every entry the
        # driver DOES accept.
        data = json.loads(ok_entry(captured_at=1.0).as_json())
        data['aweme_list'] = None
        with pytest.raises(ValueError):
            SpoolEntry.from_mapping(data)

    def test_a_bool_captured_at_is_not_a_number(self):
        data = json.loads(ok_entry(captured_at=1.0).as_json())
        data['captured_at'] = True
        with pytest.raises(ValueError):
            SpoolEntry.from_mapping(data)


class TestFileSpoolCensus:
    def test_it_lists_only_this_user_s_entries(self, tmp_path):
        write_entry(str(tmp_path), ok_entry(captured_at=1.0))
        write_entry(str(tmp_path), ok_entry(OTHER_USER_ID, captured_at=1.0))
        names = FileSpool(str(tmp_path)).entry_names(USER_ID)
        assert len(names) == 1 and all(name.startswith(f'{USER_ID}-') for name in names)

    def test_a_key_is_not_confused_with_a_longer_key_sharing_its_prefix(self):
        # Without the `-` separator, key `123` would match `1234-...json`.
        short, long = '123', '1234'
        assert not entry_name(long, 1.0).startswith(f'{short}-')

    def test_an_absent_directory_is_an_empty_census_and_not_an_error(self, tmp_path):
        assert FileSpool(str(tmp_path / 'never-created')).entry_names(USER_ID) == frozenset()


class TestNewestSince:
    """The eligibility rule, which is one half of the driver's stale-entry
    guarantee. See `test_device_driver.py` for the other half (the census is
    taken BEFORE the intent)."""

    def test_it_returns_the_newest_eligible_entry(self, tmp_path):
        write_entry(str(tmp_path), ok_entry(captured_at=10.0, ids=[1]))
        write_entry(str(tmp_path), ok_entry(captured_at=30.0, ids=[3]))
        write_entry(str(tmp_path), ok_entry(captured_at=20.0, ids=[2]))
        entry = FileSpool(str(tmp_path)).newest_since(USER_ID, after=5.0)
        assert entry is not None and entry.captured_at == 30.0

    def test_a_censused_file_is_never_eligible_however_new_its_stamp(self, tmp_path):
        # The clock-free half: an entry present before the visit is excluded by
        # NAME, so no timestamp can rehabilitate it.
        write_entry(str(tmp_path), ok_entry(captured_at=99.0))
        spool = FileSpool(str(tmp_path))
        census = spool.entry_names(USER_ID)
        assert census
        assert spool.newest_since(USER_ID, after=0.0, exclude=census) is None
        assert spool.newest_since(USER_ID, after=0.0) is not None, 'without the census it would be served'

    def test_an_entry_stamped_before_the_visit_is_rejected(self, tmp_path):
        # The timestamp half: it closes the gap the census cannot see — a
        # previous visit's response landing between the census and the intent.
        write_entry(str(tmp_path), ok_entry(captured_at=10.0))
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=20.0) is None

    def test_an_entry_stamped_exactly_at_the_visit_is_accepted(self, tmp_path):
        # `>=`, so a fast response on a coarse clock is not thrown away. The
        # census already covers anything that predates the visit by name.
        write_entry(str(tmp_path), ok_entry(captured_at=20.0))
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=20.0) is not None

    def test_a_file_whose_body_names_another_user_is_rejected(self, tmp_path):
        # Belt and braces on percent-encoding: eligibility is decided on the
        # entry's OWN `user_id`, never on the file name alone.
        entry = ok_entry(OTHER_USER_ID, captured_at=10.0)
        (tmp_path / entry_name(USER_ID, 10.0)).write_text(entry.as_json(), encoding='utf-8')
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=0.0) is None

    def test_an_unusable_file_is_skipped_rather_than_failing_the_visit(self, tmp_path, caplog):
        caplog.set_level('WARNING', logger='tiktoksearch.harvest_spool')
        (tmp_path / entry_name(USER_ID, 10.0)).write_text('{ truncated', encoding='utf-8')
        write_entry(str(tmp_path), ok_entry(captured_at=20.0, ids=[7]))
        entry = FileSpool(str(tmp_path)).newest_since(USER_ID, after=0.0)
        assert entry is not None and entry.captured_at == 20.0
        assert 'skipping unusable spool entry' in caplog.text
        assert 'truncated' not in caplog.text, 'the file NAME is logged, never its content'

    def test_a_renamed_file_still_carrying_a_real_entry_is_read(self, tmp_path):
        # The name stamp is a PREFILTER and fails open. A hand-renamed file
        # must not become invisible.
        written = Path(write_entry(str(tmp_path), ok_entry(captured_at=30.0)))
        written.rename(tmp_path / f'{USER_ID}-hand-renamed{ENTRY_SUFFIX}')
        entry = FileSpool(str(tmp_path)).newest_since(USER_ID, after=0.0)
        assert entry is not None and entry.captured_at == 30.0

    def test_a_name_stamp_older_than_the_visit_is_skipped_cheaply(self, tmp_path):
        write_entry(str(tmp_path), ok_entry(captured_at=10.0))
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=25.0) is None

    def test_nothing_yet_is_none_and_not_an_empty_feed(self, tmp_path):
        assert FileSpool(str(tmp_path)).newest_since(USER_ID, after=0.0) is None

    def test_an_unreadable_entry_is_still_returned_so_the_driver_can_tell(self, tmp_path):
        # `newest_since` must NOT filter unreadable entries out: "a response
        # arrived and we could not read it" is the fact the driver reports as
        # `UnreadableResponse` rather than as a timeout.
        write_entry(str(tmp_path), SpoolEntry.from_response(user_id=USER_ID, captured_at=10.0, body=b''))
        entry = FileSpool(str(tmp_path)).newest_since(USER_ID, after=0.0)
        assert entry is not None and not entry.is_ok and entry.reason == REASON_EMPTY
