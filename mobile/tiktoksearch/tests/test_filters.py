"""Unit tests for the pure domain layer (no network).

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch import (  # noqa: E402
    PublishTime,
    SearchFilters,
    SearchKind,
    SearchQuery,
    SortType,
)
from tiktoksearch.mapping import flatten_user, flatten_video  # noqa: E402


class TestSearchFilters:
    def test_empty_filters_produce_no_params(self):
        assert SearchFilters().is_empty()
        assert SearchFilters().to_query_params() == {}

    def test_filters_serialize_to_tiktok_wire_format(self):
        params = SearchFilters(
            sort_type=SortType.MOST_LIKED, publish_time=PublishTime.LAST_MONTH
        ).to_query_params()
        assert params["is_filter_search"] == "1"
        selected = json.loads(params["filter_selected"])
        assert selected == {"filter_by": "0", "sort_type": "1", "publish_time": "30"}

    def test_partial_filters_omit_unset_fields(self):
        params = SearchFilters(publish_time=PublishTime.LAST_WEEK).to_query_params()
        selected = json.loads(params["filter_selected"])
        assert "sort_type" not in selected
        assert selected["publish_time"] == "7"


class TestSearchQuery:
    def test_hashtag_keyword_gets_prefix(self):
        q = SearchQuery(kind=SearchKind.HASHTAG, term="bitcoin")
        assert q.keyword == "#bitcoin"
        assert q.source_term == "#bitcoin"

    def test_keyword_source_term(self):
        q = SearchQuery(kind=SearchKind.KEYWORD, term="  climate  ")
        assert q.term == "climate"          # normalized
        assert q.source_term == "search:climate"

    def test_user_source_term(self):
        assert SearchQuery(kind=SearchKind.USER, term="nasa").source_term == "user:nasa"

    def test_empty_term_rejected(self):
        with pytest.raises(ValueError):
            SearchQuery(kind=SearchKind.KEYWORD, term="   ")

    def test_bad_limit_rejected(self):
        with pytest.raises(ValueError):
            SearchQuery(kind=SearchKind.KEYWORD, term="x", limit=0)

    def test_filters_on_user_search_rejected(self):
        with pytest.raises(ValueError):
            SearchQuery(
                kind=SearchKind.USER, term="nasa",
                filters=SearchFilters(sort_type=SortType.MOST_LIKED),
            )


class TestMapping:
    def test_flatten_video_extracts_core_fields(self):
        raw = {
            "aweme_id": "123", "desc": "hi #x", "create_time": 1700000000,
            "author": {"unique_id": "bob", "uid": "42"},
            "statistics": {"play_count": 10, "digg_count": 5,
                           "comment_count": 2, "share_count": 1},
            "cha_list": [{"cha_name": "x"}],
            "added_sound_music_info": {"id": "7", "title": "song"},
        }
        rec = flatten_video(raw, "search:x")
        assert rec["id"] == "123"
        assert rec["author_username"] == "bob"
        assert rec["author_id"] == "42"
        assert rec["view_count"] == 10
        assert rec["hashtags"] == ["x"]
        assert rec["music_title"] == "song"
        assert rec["source_term"] == "search:x"

    def test_flatten_video_without_id_is_none(self):
        assert flatten_video({"desc": "no id"}, "t") is None

    def test_flatten_user_extracts_core_fields(self):
        raw = {"uid": "9", "unique_id": "nasa", "nickname": "NASA",
               "follower_count": 100, "enterprise_verify_reason": "org"}
        rec = flatten_user(raw, "user:nasa")
        assert rec["username"] == "nasa"
        assert rec["follower_count"] == 100
        assert rec["verified"] is True
