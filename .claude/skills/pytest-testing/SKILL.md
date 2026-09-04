---
name: pytest-testing
description: How to write and run tests for tiktok-searcher (class-based pytest, plain assert, stub the network); use for any test, unit test, pytest, stub, mock, fixture, coverage, regression, or TDD work.
---
# pytest-testing

Tests live in `mobile/tiktoksearch/tests/`. Run them from `mobile/` with the venv:

```
cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
```

12 tests currently pass. No lint/typecheck/coverage tool is configured — don't invent one.

## Style (match exactly)
- Class-based: `class TestConfig:`, `class TestPool:` … one class per module/behavior.
- Plain `assert`, no `unittest`, no third-party assertion libs.
- Expected failures: `with pytest.raises(ValueError):` (config/filters validation raises `ValueError`; pool exhaustion raises `errors.PoolExhausted`).
- Test only pure logic. Cross-ref `agent_docs/testing.md`.

## THE ABSOLUTE RULE — never touch the network
A test must NEVER hit TikTok or RapidAPI. There is no exception. Stub at the two seams:
- `TikTokClient._get_signed` → return canned page dicts (the raw TikTok response shape).
- `RapidSigner.sign` → return a dummy headers dict.

If a test would make a real HTTP call, it is wrong — restructure it to stub one of those seams.

## Per-module recipes
- **config.py**: build via `Config.from_mapping({...})`; assert defaults; assert `with_overrides(...)` returns a new object with only the changed field; assert the dataclass is frozen (mutating a field raises).
- **filters.py**: `SearchFilters.to_query_params()` produces the expected dict; `SearchQuery` rejects empty/invalid input with `pytest.raises(ValueError)`; exercise `SearchPage` enums.
- **identity_manager.py**: write `identities.json` to a `tempfile`, then bump mtime with `os.utime` to trigger hot-reload; assert health transitions; assert **same-cred reload preserves** state and **new-cred reload resets** state.
- **pool.py**: assert one `DeviceSlot` per identity; a stale slot is skipped; **all-stale → `PoolExhausted`**; daily cap enforced and rolls at UTC midnight — inject a fake client and a fake "now" to test the roll.
- **mapping.py**: `flatten_video`/`flatten_user` skip non-video/non-dict entries; `to_int` coerces safely (bad input → default, not crash).
- **client.py**: test `_paginate_into` dedup (same aweme id across pages counted once) and stop conditions (`has_more=false`, or a non-advancing cursor) with a stubbed `_get_signed`.

## Fake objects
Keep fakes minimal and local to the test module.

```python
class FakeSigner:
    def sign(self, *a, **k):
        return {"x-argus": "stub", "x-ss-stub": "0" * 32}

class FakeClient:
    device_id = "123"
    iid = "456"
    proxy = None
    def search(self, *a, **k):
        return {"data": [], "has_more": False}

def make_response(*, empty=False):
    # canned TikTok response — exercises success AND hit_shark
    if empty:
        return {"data": [], "search_item_list": [], "has_more": False,
                "log_pb": {"impr_id": "x"}, "search_nil_info": {"if_nil": True}}
    return {"data": [{"aweme_info": {"aweme_id": "1"}}],
            "search_item_list": [{"aweme_info": {"aweme_id": "1"}}],
            "has_more": True, "log_pb": {"impr_id": "x"}}
```

Use `empty=True` to drive the hit_shark path (empty `data`/`search_item_list` with `search_nil_info`) and the normal dict to drive the success path.

## Example
```python
class TestPaginate:
    def test_dedup_and_stop(self, monkeypatch):
        client = _make_client()
        pages = [make_response(), make_response(empty=True)]
        monkeypatch.setattr(client, "_get_signed", lambda *a, **k: pages.pop(0))
        out = []
        client._paginate_into(out, query=..., max_pages=5)
        assert len(out) == 1          # deduped, stopped on has_more=False
```
