from __future__ import annotations
import json
from dataclasses import dataclass, field
from enum import Enum

class SearchKind(str, Enum):
    KEYWORD = 'keyword'
    HASHTAG = 'hashtag'
    USER = 'user'

class SortType(str, Enum):
    RELEVANCE = '0'
    MOST_LIKED = '1'

class PublishTime(str, Enum):
    ALL_TIME = '0'
    LAST_24H = '1'
    LAST_WEEK = '7'
    LAST_MONTH = '30'
    LAST_3_MONTHS = '90'
    LAST_6_MONTHS = '180'

@dataclass(frozen=True, slots=True)
class SearchFilters:
    sort_type: SortType | None = None
    publish_time: PublishTime | None = None

    def is_empty(self) -> bool:
        return self.sort_type is None and self.publish_time is None

    def to_query_params(self) -> dict[str, str]:
        if self.is_empty():
            return {}
        # Real TikTok v46 reads filters as FLAT params (publish_time / sort_type /
        # general_filter_sort_type), NOT a filter_selected JSON blob (that was the
        # old v32 scheme and is ignored by v46 → unfiltered results). We emit the
        # flat params (what the app really sends) plus the legacy blob for the old
        # synthetic path's backward compatibility.
        params: dict[str, str] = {'is_filter_search': '1', 'filter_by': '0'}
        selected = {'filter_by': '0'}
        if self.sort_type is not None:
            params['sort_type'] = self.sort_type.value
            params['general_filter_sort_type'] = self.sort_type.value
            selected['sort_type'] = self.sort_type.value
        if self.publish_time is not None:
            params['publish_time'] = self.publish_time.value
            selected['publish_time'] = self.publish_time.value
        params['filter_selected'] = json.dumps(selected, separators=(',', ':'))
        return params

@dataclass(frozen=True, slots=True)
class SearchQuery:
    kind: SearchKind
    term: str
    limit: int = 30
    cursor: int = 0
    filters: SearchFilters = field(default_factory=SearchFilters)

    def __post_init__(self) -> None:
        if not self.term or not self.term.strip():
            raise ValueError('search term must be non-empty')
        if self.limit < 1:
            raise ValueError('limit must be >= 1')
        if self.cursor < 0:
            raise ValueError('cursor must be >= 0')
        if self.kind is SearchKind.USER and (not self.filters.is_empty()):
            raise ValueError('filters are not supported for user search')
        object.__setattr__(self, 'term', self.term.strip())

    @property
    def keyword(self) -> str:
        if self.kind is SearchKind.HASHTAG:
            return self.term if self.term.startswith('#') else f'#{self.term}'
        return self.term

    @property
    def source_term(self) -> str:
        if self.kind is SearchKind.HASHTAG:
            return self.keyword
        if self.kind is SearchKind.USER:
            return f'user:{self.term}'
        return f'search:{self.term}'

@dataclass(frozen=True, slots=True)
class SearchPage:
    records: list[dict]
    cursor: int
    next_cursor: int | None
    has_more: bool
