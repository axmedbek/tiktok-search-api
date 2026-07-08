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
        selected = {'filter_by': '0'}
        if self.sort_type is not None:
            selected['sort_type'] = self.sort_type.value
        if self.publish_time is not None:
            selected['publish_time'] = self.publish_time.value
        return {'filter_selected': json.dumps(selected, separators=(',', ':')), 'is_filter_search': '1'}

@dataclass(frozen=True, slots=True)
class SearchQuery:
    kind: SearchKind
    term: str
    limit: int = 30
    filters: SearchFilters = field(default_factory=SearchFilters)

    def __post_init__(self) -> None:
        if not self.term or not self.term.strip():
            raise ValueError('search term must be non-empty')
        if self.limit < 1:
            raise ValueError('limit must be >= 1')
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
