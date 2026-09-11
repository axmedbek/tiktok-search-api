"""Diff the real TikTok app's query params against the ones this client sends.

`/aweme/v1/aweme/post/` and `/aweme/v1/user/profile/other/` are LIVE upstream
handlers that reject this client's param set and say nothing about why — a
bodyless HTTP 200 (see `agent_docs/architecture.md` § data flow invariants).
Headers, content-encoding, host/region and the whole web route are already ruled
out by measurement, so the missing ingredient is a query parameter, and guessing
its name is unbounded. Putting the app's own query string beside ours settles it
in one sitting; this module is the settling.

Usage:
    mitmdump -s capture_requests_addon.py --set capture_out=captured_requests.jsonl
    # …open a profile in the app and scroll the post grid, then:
    ../.venv/bin/python -m tiktoksearch.capture_diff captured_requests.jsonl

It imports NO mitmproxy, so it is unit-testable. It DOES import `.client` — the
whole point is to diff against what `client.py` really builds — which pulls the
full app dep set (`requests`, `pycryptodome`, `gmssl`, `PyYAML`) and pins this
half of the tool to the project `.venv`. That is why the addon does not import
it: everything `capture_requests_addon.py` needs lives in `capture_record.py`,
which is stdlib-only, and this module imports the same pieces from there.

Two properties to keep in mind when reading a diff:

- **Values are masked, names never are.** A committed capture file with a live
  `device_id` in it is a burned identity (`.claude/rules/security.md`), and the
  diff is about which NAMES are present anyway.
- **`expected_params` covers the per-call params only.** `_common_params`
  layers the warm identity's captured `device_query` (plus `device_id`/`iid`/
  `aid` and fresh `ts`/`_rticket`) underneath every request, so those names are
  not part of an expectation, they are a replay — and the report must not call
  them missing.

  The exact replay set is the KEY NAMES of ONE identity's `device_query`, plus
  the five `_common_params` sends unconditionally, and nothing else. The
  "one identity" is load-bearing and is the trap this module has to keep
  avoiding: `_common_params` does `dict(cfg.device_query)` for the single
  identity its client was built from, so unioning `device_query` keys across a
  heterogeneous identity file credits identity A with a key only identity B
  carries — and a genuine suspect then prints `[replayed]`, i.e. the tool hides
  the one thing it exists to find. `--identity <device_id|index>` names the
  entry; without it the report intersects across entries, because the pool
  chooses the slot and an intersection can only ever UNDER-claim `[replayed]`.
  Over-claiming is the unsafe direction; under-claiming merely leaves a
  non-suspect visible in the suspect list, which costs a glance.

  When the identity file is unreadable the report falls back to *the app's
  params on the search paths we already call* and marks those
  `[also-on-search]` — a weaker, honest statement. The two sets are NOT the
  same: `device_query` is a pure device fingerprint, while a request-context
  param such as `enter_from` or `pull_type` rides on both the app's search
  request and its post-grid request without ever entering `device_query`. That
  is also the likeliest class of param a bodyless-200 validator demands, so the
  fallback marker must never read as "not a gap" — doing so would hide exactly
  the answer this tool exists to find. The five unconditional `_common_params`
  names keep their full `[replayed]` marker even in that fallback, because they
  are a fact about our own client rather than about the file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import IO, Collection, Mapping, Sequence

import yaml

# The capture-side half: the record schema and the masking the addon applies on
# the way in. Stdlib-only by design, because `mitmdump` usually runs outside
# this project's .venv — see `capture_record.py`'s docstring.
from .capture_record import (DEFAULT_CAPTURE_OUT, MASK_ELLIPSIS, MASK_KEEP_HEAD,
                             MASK_KEEP_TAIL, CapturedRequest, load_capture,
                             mask_params)
# The param builders are module-private in `client.py` and imported here on
# purpose: the whole point of this tool is to diff the app against what
# `client.py` REALLY builds. A local copy of those dicts would make the tool
# lie the moment either side drifts.
from .client import (DIRECT_PAGE_COUNT, POSTS_PAGE_COUNT, POSTS_START_CURSOR,
                     SEARCH_ITEM_PATH, SEARCH_PATHS, SEARCH_USER_PATH,
                     SEARCH_VIDEO_PATH, USER_POSTS_PATH, USER_PROFILE_PATH,
                     _posts_params, _profile_params, _user_search_params,
                     _video_search_params)
from .filters import PublishTime, SearchFilters, SortType

logger = logging.getLogger('tiktoksearch.capture_diff')

# --- Warm-identity replay set ------------------------------------------------
# Where the identity file lives, resolved with the SAME precedence the server
# uses (`api/app.py` `_resolve_identities_path`): an explicit path wins, then
# the env override, then the config's `identities_path` relative to the
# config's own directory, then a default beside the config. Mirrored rather
# than imported because `api/app.py` pulls in FastAPI and this tool must run
# from a bare capture environment.
IDENTITIES_ENV = 'TIKTOK_IDENTITIES_PATH'
DEFAULT_IDENTITIES_NAME = 'identities.json'
IDENTITIES_CONFIG_KEY = 'identities_path'
# The working profile (`CLAUDE.md`): `config_signed.yaml` is the cold legacy path.
DEFAULT_CONFIG_NAME = 'config_direct.yaml'
IDENTITY_ENTRIES_KEY = 'identities'
IDENTITY_DEVICE_QUERY_KEY = 'device_query'
# `identity_manager._identity_key` reads this, falling back to `device_query`'s
# copy of it. Mirrored so `--identity` addresses the same entries, under the
# same keys, that `IdentityStore` actually keeps.
IDENTITY_DEVICE_ID_KEY = 'device_id'
# What `_common_params` adds on top of `device_query` for every direct request.
# These five are a fact about our OWN client, independent of any file: it
# `setdefault`s device_id/iid/aid and assigns ts/_rticket outright, so they go
# out on every direct request whether or not an identity file is readable. That
# is why they are unioned into the replay set in BOTH branches of
# `build_replay_set`, not only the exact one.
COMMON_PARAM_NAMES: frozenset[str] = frozenset(('device_id', 'iid', 'aid', 'ts', '_rticket'))

# A filtered video search sends six more params than the unfiltered one that
# `expected_params` builds (`filters.py` `to_query_params`). A capture taken
# with a filter applied would otherwise report every one of them as a param the
# client never sends — a false lead in the one list that has to stay
# trustworthy. Both fields are set so the union of names is complete; the
# VALUES are irrelevant here, only the names are used.
FILTER_PARAM_NAMES: frozenset[str] = frozenset(
    SearchFilters(sort_type=SortType.RELEVANCE,
                  publish_time=PublishTime.ALL_TIME).to_query_params())

# --- Report markers (defined once; the legend renders these same constants) --
MARK_SUSPECT = '[SUSPECT]'
MARK_REPLAYED = '[replayed]'
MARK_ALSO_ON_SEARCH = '[also-on-search]'
MARK_UNDER_FILTERS = '[under-filters]'
MARK_WIDTH = max(len(mark) for mark in
                 (MARK_SUSPECT, MARK_REPLAYED, MARK_ALSO_ON_SEARCH, MARK_UNDER_FILTERS))

# The value a caller supplies at runtime (a handle, an id, a cursor). It stands
# in for one in `expected_params`, because the diff is about names — a value
# difference on one of these is noise, and the report says so.
CALLER_VALUE = '<caller>'
FIRST_PAGE_CURSOR = 0

EXIT_OK = 0
EXIT_NO_CAPTURE = 2
# An `--identities` / `--identity` the operator stated explicitly that cannot
# supply the exact replay set. Distinct from EXIT_NO_CAPTURE because the capture
# is fine and only the identity side of the answer is missing. See `main`.
EXIT_BAD_IDENTITIES = 3


@dataclass(frozen=True, slots=True)
class ParamDiff:
    """What the app sends versus what this client sends, for one path.

    `missing` is the answer being hunted: a param the app sends and we do not.
    `extra` is what we send and it does not (a param we invented, which an
    upstream validator may be rejecting outright). `differing` is a value
    disagreement on a name both sides send. Every value here obeys
    `mask_value`."""
    missing: Mapping[str, str]
    extra: Mapping[str, str]
    differing: Mapping[str, tuple[str, str]]

    @property
    def is_identical(self) -> bool:
        return not (self.missing or self.extra or self.differing)


def diff_params(app: Mapping[str, str], ours: Mapping[str, str]) -> ParamDiff:
    """Diff one app request's params against one of ours. Sorted by name, so
    two runs over the same capture render identically.

    BOTH sides are masked before they are compared, and that is load-bearing:
    the app's values arrive already masked (masking happens on the way into the
    capture file) while ours do not, so comparing raw-against-masked would
    report every sensitive name both sides send as `differing` — a guaranteed
    false hit the moment a sensitive name enters `expected_params`. The cost is
    that a sensitive name is only compared at mask granularity: a difference
    hidden inside the elided middle is invisible by design, which is the right
    trade for values that are per-device and therefore always differ anyway."""
    app_masked = mask_params(app)
    ours_masked = mask_params(ours)
    return ParamDiff(
        missing={name: app_masked[name] for name in sorted(app_masked) if name not in ours_masked},
        extra={name: ours_masked[name] for name in sorted(ours_masked) if name not in app_masked},
        differing={name: (app_masked[name], ours_masked[name]) for name in sorted(app_masked)
                   if name in ours_masked and app_masked[name] != ours_masked[name]},
    )


class CallKind(str, Enum):
    """The calls this client makes, one per param set."""
    VIDEO_SEARCH = 'video_search'
    USER_SEARCH = 'user_search'
    PROFILE = 'profile'
    POSTS = 'posts'


@dataclass(frozen=True, slots=True)
class ExpectedCall:
    """The params `client.py` builds for one call kind, and the path(s) it
    sends them to. `video_search` has two paths because the merged endpoints
    (`single/` + `search/item/`) are driven by ONE builder."""
    kind: CallKind
    paths: tuple[str, ...]
    params: Mapping[str, str]


def expected_params(kind: CallKind | str) -> ExpectedCall:
    """What `client.py` builds for `kind` — built BY `client.py`'s own builders.

    Caller-supplied values (a keyword, a user id, a cursor) stand in as
    `CALLER_VALUE`; counts and fixed params are the real constants. What is
    deliberately NOT here is `_common_params`: the warm identity's captured
    `device_query` plus `device_id`/`iid`/`aid` and fresh `ts`/`_rticket`,
    which is a replay rather than an expectation of ours (see the module
    docstring, and `replay_set`).

    Every kind is matched explicitly and an unhandled one raises: falling
    through to a default branch would silently diff a fifth call kind against
    the post-grid param set and report the difference as a finding."""
    call = CallKind(kind)
    if call is CallKind.VIDEO_SEARCH:
        return ExpectedCall(kind=call, paths=(SEARCH_VIDEO_PATH, SEARCH_ITEM_PATH),
                            params=_video_search_params(CALLER_VALUE, FIRST_PAGE_CURSOR,
                                                        DIRECT_PAGE_COUNT,
                                                        SearchFilters().to_query_params()))
    if call is CallKind.USER_SEARCH:
        return ExpectedCall(kind=call, paths=(SEARCH_USER_PATH,),
                            params=_user_search_params(CALLER_VALUE, FIRST_PAGE_CURSOR,
                                                       DIRECT_PAGE_COUNT))
    if call is CallKind.PROFILE:
        return ExpectedCall(kind=call, paths=(USER_PROFILE_PATH,),
                            params=_profile_params(CALLER_VALUE, CALLER_VALUE))
    if call is CallKind.POSTS:
        return ExpectedCall(kind=call, paths=(USER_POSTS_PATH,),
                            params=_posts_params(CALLER_VALUE, POSTS_START_CURSOR,
                                                 POSTS_PAGE_COUNT))
    raise ValueError(f'no expected param set for call kind {call.value!r}')


def our_paths() -> frozenset[str]:
    """Every upstream path this client calls."""
    return frozenset(path for kind in CallKind for path in expected_params(kind).paths)


def path_counts(records: Sequence[CapturedRequest]) -> Counter[str]:
    """How many requests the capture holds per path."""
    return Counter(record.path for record in records)


def observed_paths(records: Sequence[CapturedRequest]) -> list[tuple[str, int]]:
    """Every path in the capture with its request count, busiest first.

    This listing is the single most valuable output: if the app fetches the
    post grid from a path this client does not call, that has to be VISIBLE,
    not inferred from a diff that came back suspiciously clean."""
    return sorted(path_counts(records).items(), key=lambda item: (-item[1], item[0]))


def merged_params(records: Sequence[CapturedRequest], path: str) -> dict[str, str]:
    """The union of param names the app sent to `path`, first value each.

    A union rather than one sample request, because a param can be page-2 only
    (a cursor is the obvious case) and a first-page sample would hide it."""
    merged: dict[str, str] = {}
    for record in records:
        if record.path != path:
            continue
        for name, value in record.params.items():
            merged.setdefault(name, value)
    return merged


@dataclass(frozen=True, slots=True)
class SearchPathParams:
    """What the app sent on the search paths this client already calls: name ->
    first value seen (masked, as everything in a capture record is).

    The values matter as well as the names. A request-context param the app
    sends on both a search path and the post grid often carries a DIFFERENT
    value on each (`enter_from=search_result` vs `others_homepage`), and a
    per-path diff that prints only the post-grid value throws that away — a
    value that changes with the request context is itself evidence about what
    the param means."""
    values: Mapping[str, str]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.values)


def search_path_params(records: Sequence[CapturedRequest],
                       paths: Collection[str]) -> SearchPathParams:
    """The app's params on `paths`, unioned first-value-wins.

    `paths` is a `Collection`, not an `Iterable`, on purpose: this iterates
    `paths` once per record, and a generator argument would be exhausted after
    the first record and silently yield an empty result."""
    wanted = frozenset(paths)
    values: dict[str, str] = {}
    for record in records:
        if record.path not in wanted:
            continue
        for name, value in record.params.items():
            values.setdefault(name, value)
    return SearchPathParams(values=values)


class ReplaySource(str, Enum):
    """Where the replay set came from — which decides how strong a claim the
    report is allowed to make about a missing name."""
    # Exact, and exact for the identity actually in play: ONE identity's own
    # `device_query` keys. `_common_params` builds `dict(cfg.device_query)` for
    # a SINGLE identity, so this set — and only this set — is what a request
    # really replays.
    DEVICE_QUERY_ONE = 'device_query'
    # Safe: the `device_query` keys EVERY entry in the file shares. Used when no
    # identity was named and the file holds more than one, because `ClientPool`
    # picks the slot and this tool cannot know which identity served the
    # request. An intersection is a subset of whichever one did, so `[replayed]`
    # is UNDER-claimed. A union across entries would be the unsafe direction: a
    # key only identity B carries would be credited to identity A, and a
    # genuine suspect would render `[replayed]` — the tool hiding the very
    # thing it exists to find.
    DEVICE_QUERY_COMMON = 'device_query_common'
    # Fallback: the app's params on the search paths we call. Correlated with
    # `device_query` but NOT equal to it — see the module docstring.
    SEARCH_PATHS = 'search_paths'


@dataclass(frozen=True, slots=True)
class ReplaySet:
    """Param names to exclude from the suspect list, and the strength of that
    exclusion."""
    names: frozenset[str]
    source: ReplaySource
    #: How many identity entries the `device_query` names were read from. 0 when
    #: the set did not come from the identity file at all.
    entries: int = 0

    @property
    def from_device_query(self) -> bool:
        """Whether the set was read from the identity file at all — i.e. whether
        `[replayed]` is a claim about our OWN request rather than a heuristic."""
        return self.source is not ReplaySource.SEARCH_PATHS

    @property
    def is_exact(self) -> bool:
        """Whether the set is exactly what the one identity in play replays."""
        return self.source is ReplaySource.DEVICE_QUERY_ONE

    @property
    def marker(self) -> str:
        return MARK_REPLAYED if self.from_device_query else MARK_ALSO_ON_SEARCH


def resolve_identities_path(explicit: str | None = None,
                            config_path: str | None = None) -> str | None:
    """Where to read the warm identities from, or None if nowhere.

    Same precedence as the server (`api/app.py` `_resolve_identities_path`),
    with an explicit CLI path ahead of it: explicit > `TIKTOK_IDENTITIES_PATH`
    > the config's `identities_path` (relative paths resolved against the
    CONFIG's directory, not the process cwd) > `identities.json` beside the
    config.

    This RESOLVES, it does not validate: an explicit path is handed back
    unchecked, deliberately, so that one code path decides where to look and a
    different one decides whether looking worked. `main` is that other path —
    it refuses to answer with the heuristic when a flag was stated
    (`EXIT_BAD_IDENTITIES`), which is what stops a typo here from degrading
    silently."""
    if explicit:
        return explicit
    env = os.environ.get(IDENTITIES_ENV)
    if env:
        return env
    config = config_path or DEFAULT_CONFIG_NAME
    config_dir = os.path.dirname(os.path.abspath(config)) or '.'
    if os.path.exists(config):
        try:
            with open(config, 'r', encoding='utf-8') as handle:
                raw = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning('cannot read %s for %s: %s', config, IDENTITIES_CONFIG_KEY, exc)
            raw = {}
        configured = raw.get(IDENTITIES_CONFIG_KEY) if isinstance(raw, Mapping) else None
        if configured:
            configured = str(configured)
            return configured if os.path.isabs(configured) else os.path.join(config_dir, configured)
    default = os.path.join(config_dir, DEFAULT_IDENTITIES_NAME)
    return default if os.path.exists(default) else None


@dataclass(frozen=True, slots=True)
class DeviceQueryNames:
    """`device_query` KEY NAMES read out of the identity file, and how they were
    derived. Names only — never a value from that file."""
    names: frozenset[str]
    source: ReplaySource
    #: How many entries the names were derived from (1 when one was selected).
    entries: int


def _entry_key(entry: Mapping[str, object]) -> str:
    """One entry's identity key, exactly as `identity_manager._identity_key`
    computes it.

    Mirrored rather than imported only to keep this tool runnable from a bare
    capture environment; the rule has to match, because an entry with no key is
    one `IdentityStore` SKIPS entirely — so it must not occupy an `--identity`
    index here either, or the index would address a different identity than the
    server's `id<n>` slot of the same number."""
    device_query = entry.get(IDENTITY_DEVICE_QUERY_KEY)
    from_query = device_query.get(IDENTITY_DEVICE_ID_KEY) if isinstance(device_query, Mapping) else None
    return str(entry.get(IDENTITY_DEVICE_ID_KEY) or from_query or '')


def _load_identity_entries(path: str) -> list[Mapping[str, object]] | None:
    """The usable entries in the identity file at `path`, in file order, or None
    when the file cannot supply any.

    Accepts the same two shapes `IdentityStore._read_file` accepts (a bare list,
    or `{"identities": [...]}`) and drops the same entries `IdentityStore` drops
    (not an object, or no identity key), so positions line up with the pool's
    `id0`, `id1`, … slots.

    None means "ask something else": the file is absent (the ordinary case on a
    machine that has never run the capture loop), unreadable, or malformed."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except OSError as exc:
        logger.info('no warm identities at %s (%s) — falling back to the search-path '
                    'heuristic for the replay set', path, exc.__class__.__name__)
        return None
    except (ValueError, TypeError) as exc:
        logger.warning('cannot parse identities file %s: %s', path, exc)
        return None
    if isinstance(data, Mapping):
        data = data.get(IDENTITY_ENTRIES_KEY) or []
    if not isinstance(data, list):
        logger.warning('identities file %s is neither a list nor {"%s": [...]}',
                       path, IDENTITY_ENTRIES_KEY)
        return None
    entries = [entry for entry in data if isinstance(entry, Mapping) and _entry_key(entry)]
    if not entries:
        logger.warning('identities file %s carries no usable identity entries — falling '
                       'back to the search-path heuristic for the replay set', path)
        return None
    return entries


def _select_entries(entries: Sequence[Mapping[str, object]], selector: str | None,
                    path: str) -> Sequence[Mapping[str, object]] | None:
    """The entries `selector` addresses — all of them when it is None, exactly
    one when it names an identity — or None when it names one that is not there.

    A `device_id` match wins over an index, so a numeric device_id is never
    shadowed by the index reading of the same string.

    The selector is NOT echoed into the log on a miss: it may BE a device_id,
    and a device_id in terminal scrollback is the leak this module masks
    everywhere else (`.claude/rules/logging.md`). Neither are the file's own
    keys listed for the same reason — the count is the whole safe part."""
    if selector is None:
        return entries
    for entry in entries:
        if _entry_key(entry) == selector:
            return (entry,)
    if selector.isdigit() and int(selector) < len(entries):
        return (entries[int(selector)],)
    logger.warning('--identity matches none of the %d identit(y/ies) in %s — expected a '
                   'device_id or a 0-based index', len(entries), path)
    return None


def device_query_names(path: str, selector: str | None = None) -> DeviceQueryNames | None:
    """The `device_query` KEY NAMES the identity at `path` replays, or None when
    the file cannot supply them.

    KEYS ONLY. This function never returns, logs or otherwise touches a value
    from that file — `identities.json` holds a live `cookie` and `x_tt_token`
    (`.claude/rules/security.md`), and a param NAME is not a secret, which is
    why reading just the keys is both sufficient and safe.

    The set is scoped to the identity actually in play, and that scoping is the
    whole point. `_common_params` does `dict(cfg.device_query)` for the ONE
    identity its client was built from, so a name is only genuinely replayed if
    THAT identity carries it:

    - `selector` given, or the file holds a single entry — the answer is that
      one entry's keys, and it is EXACT.
    - otherwise — the answer is the INTERSECTION over every entry. The pool
      chooses the slot, so this tool cannot know which identity served the
      request; the intersection is a subset of whichever one did, which
      under-claims `[replayed]` and can therefore only ever turn a
      false-innocent back into a visible suspect. Unioning instead would credit
      identity A with a key only identity B has and mark a real suspect
      `[replayed]` — silently hiding the answer being hunted.

    An empty intersection is a legitimate answer and is returned as one (every
    name then reads as a suspect, the loud direction). Only an unusable FILE
    yields None: absent, malformed, or carrying no `device_query` at all —
    asserting exactness off nothing would be a claim the file does not support."""
    entries = _load_identity_entries(path)
    if entries is None:
        return None
    selected = _select_entries(entries, selector, path)
    if selected is None:
        return None
    per_entry = [frozenset(str(name) for name in entry[IDENTITY_DEVICE_QUERY_KEY])
                 for entry in selected
                 if isinstance(entry.get(IDENTITY_DEVICE_QUERY_KEY), Mapping)]
    if not per_entry or not frozenset().union(*per_entry):
        logger.warning('identities file %s carries no %s keys for the selected '
                       'identit(y/ies) — falling back to the search-path heuristic '
                       'for the replay set', path, IDENTITY_DEVICE_QUERY_KEY)
        return None
    if len(per_entry) == 1:
        return DeviceQueryNames(names=per_entry[0], source=ReplaySource.DEVICE_QUERY_ONE,
                                entries=1)
    return DeviceQueryNames(names=frozenset.intersection(*per_entry),
                            source=ReplaySource.DEVICE_QUERY_COMMON, entries=len(per_entry))


def build_replay_set(on_search: SearchPathParams, identities_path: str | None,
                     selector: str | None = None) -> ReplaySet:
    """The names the report may exclude from the suspect list, preferring the
    identity file's `device_query` keys and falling back to the app's
    search-path params when it is unavailable.

    `COMMON_PARAM_NAMES` is unioned into BOTH branches, and that is not
    symmetry for its own sake: `_common_params` sends `device_id`, `iid`,
    `aid`, `ts` and `_rticket` unconditionally, with or without a readable
    identity file. Leaving them out of the fallback rendered five names we
    provably DO send as `[SUSPECT]` — five false leads in the one list that has
    to stay trustworthy, and enough to bury the real one. Unioning them can
    hide nothing, because they are the one group whose presence does not depend
    on the file."""
    if identities_path:
        from_file = device_query_names(identities_path, selector)
        if from_file is not None:
            return ReplaySet(names=from_file.names | COMMON_PARAM_NAMES,
                             source=from_file.source, entries=from_file.entries)
    return ReplaySet(names=on_search.names | COMMON_PARAM_NAMES,
                     source=ReplaySource.SEARCH_PATHS)


def _marker(name: str, replay: ReplaySet, filtered: Collection[str]) -> str:
    """The marker for one missing param name.

    Ordered by how provable each claim is, strongest first. `filtered` and
    `COMMON_PARAM_NAMES` are both facts about our own client that hold whatever
    the identity file says, so they outrank the replay set and are marked at
    full strength even when the set itself is only the search-path heuristic —
    which is what keeps those five out of the suspect list in fallback mode."""
    if name in filtered:
        return MARK_UNDER_FILTERS
    if name in COMMON_PARAM_NAMES:
        return MARK_REPLAYED
    if name in replay.names:
        return replay.marker
    return MARK_SUSPECT


def _cross_path_note(name: str, value: str, cross_path: Mapping[str, str]) -> str:
    """The trailing note for a param the app also sent elsewhere with a
    different value. Empty when there is nothing to add."""
    other = cross_path.get(name)
    if other is None or other == value:
        return ''
    return f'   (a search request sent {other!r} instead)'


def _render_diff(diff: ParamDiff, replay: ReplaySet, cross_path: Mapping[str, str],
                 filtered: Collection[str]) -> list[str]:
    lines: list[str] = []
    if diff.is_identical:
        return ['    params are identical']
    if diff.missing:
        lines.append('    missing (the app sends it, we do not):')
        for name, value in diff.missing.items():
            mark = _marker(name, replay, filtered)
            lines.append(f'      {mark:<{MARK_WIDTH}} {name} = {value}'
                         + _cross_path_note(name, value, cross_path))
    if diff.extra:
        lines.append('    extra (we send it, the app does not):')
        lines.extend(f'      {name} = {value}' for name, value in diff.extra.items())
    if diff.differing:
        lines.append('    differing (both send it):')
        lines.extend(f'      {name}: app={app_value}  ours={our_value}'
                     for name, (app_value, our_value) in diff.differing.items())
    return lines


def _replay_header(replay: ReplaySet, identities_path: str | None,
                   selector: str | None = None) -> list[str]:
    """Where the replay set came from, stated precisely — because which marker
    the report uses, and how much it is worth, depends entirely on that.

    Precisely means naming the SCOPE and not just the source: "the
    `device_query` of one identity" and "the keys every identity shares" are
    different claims with different failure modes, and a header that blurs them
    is how an over-approximation gets read as an exact answer."""
    common = len(COMMON_PARAM_NAMES)
    if replay.is_exact:
        whose = ('the identity you named' if selector is not None
                 else 'the only identity in the file')
        return [f'replay set: {len(replay.names)} names — the {IDENTITY_DEVICE_QUERY_KEY} keys of '
                f'{whose} ({identities_path}),',
                f'  plus the {common} _common_params always adds. EXACT for that identity: it is what '
                '_common_params replays.']
    if replay.source is ReplaySource.DEVICE_QUERY_COMMON:
        return [f'replay set: {len(replay.names)} names — the {IDENTITY_DEVICE_QUERY_KEY} keys shared by ALL '
                f'{replay.entries} identities in {identities_path},',
                f'  plus the {common} _common_params always adds. No identity was named and the pool picks '
                'the slot, so this',
                '  is an INTERSECTION: a subset of whatever the identity in play really replays, which '
                'under-claims',
                f'  {MARK_REPLAYED} rather than over-claiming it. Pass --identity <device_id|index> for the '
                'exact set.']
    where = ('no identity file found' if identities_path is None
             else f'no usable identity file at {identities_path}')
    heuristic = len(replay.names - COMMON_PARAM_NAMES)
    return [f'replay set: {len(replay.names)} names — the {common} _common_params always sends '
            f'(provable, {MARK_REPLAYED}) plus',
            f'  {heuristic} more seen on the app’s own search paths ({where}). Only the first {common} '
            'are a claim about',
            '  our request; the rest is a HEURISTIC, see the legend.']


def _legend(replay: ReplaySet) -> list[str]:
    # The marker column is `'  '` + a MARK_WIDTH-padded marker + `' '`; wrapped
    # lines hang under the text, not under the marker.
    pad = ' ' * (MARK_WIDTH + 3)
    common = ', '.join(sorted(COMMON_PARAM_NAMES))
    lines = ['', 'How to read this:',
             f'  {MARK_SUSPECT:<{MARK_WIDTH}} the app sends it and NOTHING we send does — the param set to try first.']
    if replay.is_exact:
        lines += [f"  {MARK_REPLAYED:<{MARK_WIDTH}} a key of the SELECTED identity's {IDENTITY_DEVICE_QUERY_KEY}, or one of the",
                  f'{pad}{len(COMMON_PARAM_NAMES)} _common_params always adds ({common})',
                  f'{pad}— replayed verbatim on every request, so genuinely not a gap.']
    elif replay.source is ReplaySource.DEVICE_QUERY_COMMON:
        lines += [f'  {MARK_REPLAYED:<{MARK_WIDTH}} a {IDENTITY_DEVICE_QUERY_KEY} key EVERY identity in the file carries, or one',
                  f'{pad}of the {len(COMMON_PARAM_NAMES)} _common_params always adds ({common})',
                  f'{pad}— replayed whichever identity the pool picked, so not a gap. The',
                  f'{pad}converse does NOT hold: a key only SOME identities carry is left in',
                  f'{pad}the suspect list on purpose, so pass --identity to clear it.']
    else:
        lines += [f'  {MARK_REPLAYED:<{MARK_WIDTH}} one of the {len(COMMON_PARAM_NAMES)} names _common_params sends unconditionally',
                  f'{pad}({common}) — true of our client with or',
                  f'{pad}without a readable identity file, so not a gap even here.',
                  f'  {MARK_ALSO_ON_SEARCH:<{MARK_WIDTH}} also sent by the app on a search path — check it against the',
                  f"{pad}identity's {IDENTITY_DEVICE_QUERY_KEY} before dismissing it. This is NOT evidence",
                  f'{pad}that we send it: a request-context param (enter_from, pull_type)',
                  f'{pad}rides on both paths without ever entering {IDENTITY_DEVICE_QUERY_KEY}, and that',
                  f'{pad}is the likeliest class of param the validator wants. Re-run with',
                  f'{pad}--identities pointed at a real identity file to get a real answer.']
    lines += [f'  {MARK_UNDER_FILTERS:<{MARK_WIDTH}} the video-search expectation here is the UNFILTERED one, and this',
              f'{pad}param is one the client does send once a filter is applied',
              f'{pad}(filters.py to_query_params) — not a gap either.',
              '  A `(a search request sent … instead)` note means the app sent that same param with a',
              '  different value on a search path. A context-dependent value is itself a clue.',
              f'  {CALLER_VALUE} is a caller-supplied value (handle, id, cursor): a `differing` line on',
              f'  one of those is expected. Sensitive values are masked as {MASK_KEEP_HEAD} chars'
              f'{MASK_ELLIPSIS}{MASK_KEEP_TAIL} chars;',
              '  param and header NAMES are never masked.',
              '  A path above without a * that looks like a post grid or a profile is the finding:',
              '  it means the app does not use the endpoint this client assumes.']
    return lines


def render_report(records: Sequence[CapturedRequest],
                  identities_path: str | None = None, *,
                  selector: str | None = None,
                  replay: ReplaySet | None = None) -> list[str]:
    """The whole report as lines, without newlines.

    Reads at most one file — the identity file at `identities_path`, and only
    its `device_query` KEY names — and writes none; the CLI writes what this
    returns. `replay` lets a caller that already built the set (the CLI, which
    has to inspect it before rendering) pass it in rather than have the file
    read, and its warnings emitted, a second time."""
    calls = our_paths()
    on_search = search_path_params(records, SEARCH_PATHS)
    if replay is None:
        replay = build_replay_set(on_search, identities_path, selector)
    counts = path_counts(records)
    paths = observed_paths(records)
    lines = [f'{len(records)} requests over {len(paths)} distinct paths',
             *_replay_header(replay, identities_path, selector), '',
             'observed paths  (* = a path this client also calls)']
    for path, count in paths:
        marker = '*' if path in calls else ' '
        lines.append(f'  {count:6d}  {marker} {path}')
    unseen = sorted(calls - counts.keys())
    if unseen:
        lines += ['', 'paths this client calls that this capture never saw:']
        lines.extend(f'    {path}' for path in unseen)
    for kind in CallKind:
        call = expected_params(kind)
        filtered = FILTER_PARAM_NAMES if kind is CallKind.VIDEO_SEARCH else frozenset()
        for path in call.paths:
            count = counts.get(path, 0)
            app = merged_params(records, path)
            lines += ['', f'--- {kind.value}  {path}']
            if not count:
                # Distinct from the next case on purpose: "the capture never saw
                # this path" and "the app called it and sent no query params"
                # are opposite answers to the question being hunted, and one
                # message for both reads as a failed capture session.
                lines.append('    not observed in this capture — nothing to diff')
                continue
            if not app:
                lines.append(f'    {count} request(s) observed, but NOT ONE carried a query '
                             'param — nothing to diff (a bodyless/param-less call is itself a finding)')
                continue
            lines.append(f'    {count} request(s) observed, {len(app)} distinct param names')
            # A search path is compared against itself in `on_search`, so the
            # cross-path note would restate the same union it came from.
            cross_path = {} if path in SEARCH_PATHS else on_search.values
            lines.extend(_render_diff(diff_params(app, call.params), replay, cross_path, filtered))
    lines += _legend(replay)
    return lines


def main(argv: Sequence[str] | None = None, *, stream: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m tiktoksearch.capture_diff',
        description="Diff the TikTok app's captured query params against the ones this client sends.")
    parser.add_argument('capture', help=f'JSONL file written by capture_requests_addon.py (default name: {DEFAULT_CAPTURE_OUT})')
    parser.add_argument('--identities', default=None,
                        help='Warm identity file to read the exact replay set from (its '
                             f'{IDENTITY_DEVICE_QUERY_KEY} KEY names only, never a value). Default: '
                             f'${IDENTITIES_ENV}, then the config\u2019s {IDENTITIES_CONFIG_KEY}, then '
                             f'{DEFAULT_IDENTITIES_NAME} beside the config. A path given HERE must work: '
                             f'the run fails with exit {EXIT_BAD_IDENTITIES} rather than quietly '
                             'answering with the heuristic instead.')
    parser.add_argument('--identity', default=None, metavar='DEVICE_ID|INDEX',
                        help='Which identity in that file replays the params \u2014 a device_id, or a '
                             '0-based index into the file (the pool\u2019s id0, id1, \u2026 order). '
                             f'Default: the {IDENTITY_DEVICE_QUERY_KEY} keys shared by ALL entries, '
                             'which under-claims rather than crediting one identity with another\u2019s '
                             f'keys. Also fails with exit {EXIT_BAD_IDENTITIES} if it matches nothing.')
    parser.add_argument('--config', default=DEFAULT_CONFIG_NAME,
                        help=f'Config to resolve {IDENTITIES_CONFIG_KEY} from (default: {DEFAULT_CONFIG_NAME}).')
    args = parser.parse_args(argv)
    out = stream if stream is not None else sys.stdout
    try:
        records = load_capture(args.capture)
    except OSError as exc:
        logger.error('cannot read capture %s: %s', args.capture, exc)
        return EXIT_NO_CAPTURE
    if not records:
        logger.error('capture %s holds no usable requests', args.capture)
        return EXIT_NO_CAPTURE
    identities_path = resolve_identities_path(args.identities, args.config)
    replay = build_replay_set(search_path_params(records, SEARCH_PATHS),
                              identities_path, args.identity)
    # An explicit flag is STATED INTENT, and degrading it to the heuristic is
    # answering a different question than the one asked. `resolve_identities_path`
    # hands back an explicit path unchecked (it resolves, it does not validate),
    # so a typo used to surface as nothing louder than a header line \u2014 and a
    # report whose markers are heuristic reads exactly like one whose markers
    # were vetted. Failing costs one re-run with the path fixed; not failing
    # costs a param hunt down the wrong list.
    stated = ' and '.join(flag for flag, value in (('--identities', args.identities),
                                                   ('--identity', args.identity)) if value)
    if stated and not replay.from_device_query:
        logger.error('%s given explicitly, but no exact replay set could be read from %s \u2014 '
                     'refusing to fall back to the search-path heuristic. Fix the path or the '
                     'selector, or drop the flag to accept the heuristic deliberately.',
                     stated, identities_path or '(nothing resolved)')
        return EXIT_BAD_IDENTITIES
    for line in render_report(records, identities_path, selector=args.identity, replay=replay):
        out.write(line + '\n')
    return EXIT_OK


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    sys.exit(main())
