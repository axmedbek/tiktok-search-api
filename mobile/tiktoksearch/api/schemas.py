from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field
from ..filters import PublishTime, SearchKind, SortType
from ..paging import MAX_PAGE_TOKEN_CHARS

class FiltersIn(BaseModel):
    sort_type: Optional[SortType] = Field(default=None, description='Result ordering. `0` = relevance (default), `1` = most liked.')
    publish_time: Optional[PublishTime] = Field(default=None, description='Recency window. `0` = all time, `1` = last 24h, `7` = last week, `30` = last month, `90` = last 3 months, `180` = last 6 months.')
    model_config = {'json_schema_extra': {'examples': [{'sort_type': '1', 'publish_time': '30'}]}}

class SearchRequest(BaseModel):
    type: SearchKind = Field(description='What to search: keyword, hashtag, or user.')
    query: str = Field(min_length=1, max_length=200, description='The search term.')
    limit: int = Field(default=30, ge=1, le=300, description='Max results per page (server-capped by `max_results_per_search`, 300 on the shipped profile). Cost scales with it: one page of results costs one signed request per ~10 raw items per endpoint, so a large `limit` buys depth in a single call at the price of latency. It spends one daily-cap unit either way, so `limit=300` in one call and `limit=30` chained are priced identically against the cap.')
    cursor: int = Field(default=0, ge=0, description='Legacy pagination offset. Kept for compatibility, but a bare offset carries no TikTok search session and comes back empty (`empty_session`) — use `page_token` instead. Start at 0.')
    page_token: Optional[str] = Field(default=None, max_length=MAX_PAGE_TOKEN_CHARS, description="Opaque continuation token from a prior response's `page_token`. It carries TikTok's own cursor plus the `search_id` search session, so the next page actually returns fresh results. It is authenticated with a per-process secret and is only valid for the same type/query/filters on the server instance that minted it: tokens do NOT survive a server restart (a TikTok search session is short-lived anyway), and a tampered or stale one is rejected with 422. It pins the device that served the previous page, so the server default `fan_out` is coerced to 1 — an explicit `fan_out` above 1 alongside a token is rejected with 422. Mutually exclusive with a non-zero `cursor`.")
    fan_out: Optional[int] = Field(default=None, ge=1, le=32, description='Query this many devices in parallel and merge+dedupe their results. Higher = more unique results per call (each device returns a shallow window), at the cost of one daily-cap unit per device. Capped at the pool size. Defaults to the server `default_fan_out` (so a plain request already returns a merged page). Ignored (forced to 1) when `page_token` is supplied, because a search session lives on a single device.')
    filters: Optional[FiltersIn] = Field(default=None, description='Optional filters (video searches only).')
    model_config = {'json_schema_extra': {'examples': [{'type': 'keyword', 'query': 'climate change', 'limit': 50, 'fan_out': 8}, {'type': 'user', 'query': 'nasa', 'limit': 10}]}}

class SearchResponse(BaseModel):
    query: str
    type: SearchKind
    device: str = Field(description='Label of the device that served the request.')
    count: int
    cursor: int = Field(description='The cursor this page started from.')
    next_cursor: Optional[int] = Field(default=None, description="TikTok's own cursor at the end of this page. Informational — pass `page_token` back, not this, to fetch the next page. Null when there are no more results.")
    page_token: Optional[str] = Field(default=None, description='Pass this back as `page_token` to fetch the next page. Null when there are no more results (or when the page cannot be resumed, e.g. a merged multi-device page).')
    has_more: bool = Field(description='Whether more results are available beyond this page.')
    elapsed_s: float
    results: list[dict[str, Any]]

class DeviceStatus(BaseModel):
    label: str
    device_id: str
    iid: str
    proxy: Optional[str] = None
    used_today: int
    daily_cap: int
    remaining_today: int
    busy: bool

class HealthResponse(BaseModel):
    status: str
    device_count: int
    idle: int
    total_daily_capacity: int
    capacity_remaining_today: int
    devices: list[DeviceStatus]
