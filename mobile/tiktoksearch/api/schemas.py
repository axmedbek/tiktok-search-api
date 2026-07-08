from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field
from ..filters import PublishTime, SearchKind, SortType

class FiltersIn(BaseModel):
    sort_type: Optional[SortType] = Field(default=None, description='Result ordering. `0` = relevance (default), `1` = most liked.')
    publish_time: Optional[PublishTime] = Field(default=None, description='Recency window. `0` = all time, `1` = last 24h, `7` = last week, `30` = last month, `90` = last 3 months, `180` = last 6 months.')
    model_config = {'json_schema_extra': {'examples': [{'sort_type': '1', 'publish_time': '30'}]}}

class SearchRequest(BaseModel):
    type: SearchKind = Field(description='What to search: keyword, hashtag, or user.')
    query: str = Field(min_length=1, max_length=200, description='The search term.')
    limit: int = Field(default=30, ge=1, le=200, description='Max results (server-capped).')
    filters: Optional[FiltersIn] = Field(default=None, description='Optional filters (video searches only).')
    model_config = {'json_schema_extra': {'examples': [{'type': 'keyword', 'query': 'climate change', 'limit': 20, 'filters': {'sort_type': '1', 'publish_time': '30'}}, {'type': 'user', 'query': 'nasa', 'limit': 10}]}}

class SearchResponse(BaseModel):
    query: str
    type: SearchKind
    device: str = Field(description='Label of the device that served the request.')
    count: int
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
