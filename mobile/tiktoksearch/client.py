from __future__ import annotations
import logging
import random
import time
import urllib.parse
from typing import Callable, Optional
import requests
from .config import ClientConfig
from .errors import RateLimited, SoftError, TransportError
from .filters import SearchKind, SearchPage, SearchQuery
from .mapping import flatten_user, flatten_video
from .rapid_signer import RapidSigner
from .signing import MetasecSigner
logger = logging.getLogger('tiktoksearch.client')
SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'
SEARCH_ITEM_PATH = '/aweme/v1/search/item/'
SEARCH_USER_PATH = '/aweme/v1/discover/search/'
_ID_LO = 7000000000000000000
_ID_HI = 7499999999999999999

def _synth_id() -> str:
    return str(random.randint(_ID_LO, _ID_HI))

class TikTokClient:

    def __init__(self, config: ClientConfig, *, signer: Optional[MetasecSigner]=None) -> None:
        self._config = config
        self._direct = bool(config.rapidapi_key)
        dq = config.device_query or {}
        self.device_id = config.device_id or dq.get('device_id') or _synth_id()
        self.iid = config.iid or dq.get('iid') or _synth_id()
        self.proxy = config.proxy
        if self._direct:
            self._signer = RapidSigner(config)
        else:
            self._signer = signer or MetasecSigner(config)
        self._session = requests.Session()
        if config.proxy:
            self._session.proxies = {'http': config.proxy, 'https': config.proxy}

    def search(self, query: SearchQuery) -> SearchPage:
        if query.kind is SearchKind.USER:
            return self._search_users(query)
        return self._search_videos(query)

    def _search_videos(self, query: SearchQuery) -> SearchPage:
        filter_params = query.filters.to_query_params()

        def build(offset: int, count: int) -> dict:
            params = {'keyword': query.keyword, 'count': str(count), 'offset': str(offset), 'search_source': 'normal_search'}
            params.update(filter_params)
            return params

        def unwrap(item: dict) -> Optional[dict]:
            return item.get('aweme_info') or item.get('aweme') or (item if item.get('aweme_id') else None)

        # Direct mode: the app fetches results from the general "Top" endpoint
        # (data[]) AND the Videos-tab endpoint (search_item_list[]). One endpoint
        # alone stops at ~30 (has_more=false); chaining both merges to more —
        # this is why the phone shows more than a single endpoint returns.
        if self._direct:
            out: list[dict] = []
            seen: set[str] = set()
            has_more = False
            for i, (path, items_key) in enumerate(((SEARCH_VIDEO_PATH, 'data'), (SEARCH_ITEM_PATH, 'search_item_list'))):
                if len(out) >= query.limit:
                    break
                try:
                    hm = self._paginate_into(out=out, seen=seen, path=path, build_params=build, items_key=items_key, unwrap=unwrap, flatten=flatten_video, source_term=query.source_term, limit=query.limit, start_cursor=query.cursor)
                    has_more = has_more or hm
                except (SoftError, TransportError) as exc:
                    # First endpoint must succeed; a failed/empty secondary
                    # (Videos tab) is tolerated — keep what the first returned.
                    if i == 0 and not out:
                        raise
                    logger.warning('secondary endpoint %s failed (kept %d results): %s', path, len(out), exc)
            return SearchPage(records=out, cursor=query.cursor, next_cursor=query.cursor + len(out) if has_more else None, has_more=has_more)

        return self._paginate(path=SEARCH_VIDEO_PATH, build_params=build, items_key='data', unwrap=unwrap, flatten=flatten_video, source_term=query.source_term, limit=query.limit, start_cursor=query.cursor)

    def _search_users(self, query: SearchQuery) -> SearchPage:

        def build(offset: int, count: int) -> dict:
            return {'keyword': query.term, 'count': str(count), 'cursor': str(offset), 'type': '1', 'search_source': 'normal_search'}
        return self._paginate(path=SEARCH_USER_PATH, build_params=build, items_key='user_list', unwrap=lambda item: item.get('user_info') or item, flatten=flatten_user, source_term=query.source_term, limit=query.limit, start_cursor=query.cursor)

    def _paginate(self, *, path: str, build_params: Callable[[int, int], dict], items_key: str, unwrap: Callable[[dict], Optional[dict]], flatten: Callable[[dict, str], Optional[dict]], source_term: str, limit: int, start_cursor: int) -> SearchPage:
        out: list[dict] = []
        seen: set[str] = set()
        has_more = self._paginate_into(out=out, seen=seen, path=path, build_params=build_params, items_key=items_key, unwrap=unwrap, flatten=flatten, source_term=source_term, limit=limit, start_cursor=start_cursor)
        return SearchPage(records=out, cursor=start_cursor, next_cursor=start_cursor + len(out) if has_more else None, has_more=has_more)

    def _paginate_into(self, *, out: list[dict], seen: set[str], path: str, build_params: Callable[[int, int], dict], items_key: str, unwrap: Callable[[dict], Optional[dict]], flatten: Callable[[dict, str], Optional[dict]], source_term: str, limit: int, start_cursor: int) -> bool:
        """Paginate one endpoint, appending unique records into `out`/`seen`.
        Returns has_more from the last page. Shared by _paginate and the direct
        multi-endpoint path so both endpoints dedupe against the same set."""
        cursor = start_cursor
        has_more = False
        search_id = ''
        # direct mode paginates deeper with count=10 (count=20 returns has_more=false early)
        page_count = 10 if self._direct else 20
        while len(out) < limit:
            params = self._common_params()
            params.update(build_params(cursor, page_count))
            # session chaining: echo the previous response's search_id (direct mode)
            if self._direct and search_id:
                params['search_id'] = search_id
            data = self._get_signed(path, params)
            raw_items = data.get(items_key) or []
            next_cursor = data.get('cursor', cursor + len(raw_items))
            has_more = bool(data.get('has_more'))
            search_id = (data.get('log_pb') or {}).get('impr_id') or (data.get('extra') or {}).get('logid') or search_id
            added = 0
            for raw in raw_items:
                node = unwrap(raw)
                if node is None:
                    continue
                record = flatten(node, source_term)
                key = record and (record.get('id') or record.get('username'))
                if not record or not key or key in seen:
                    continue
                seen.add(key)
                out.append(record)
                added += 1
                if len(out) >= limit:
                    break
            cursor = next_cursor
            if not has_more or not raw_items:
                break
            # guard against non-advancing pagination
            if added == 0 and cursor == start_cursor:
                break
        return has_more

    def _common_params(self) -> dict:
        cfg = self._config
        if self._direct:
            # full warm device fingerprint + fresh per-request timestamps
            now = int(time.time())
            params = dict(cfg.device_query or {})
            params.setdefault('device_id', self.device_id)
            params.setdefault('iid', self.iid)
            params.setdefault('aid', str(cfg.app_id))
            params['ts'] = str(now)
            params['_rticket'] = str(now * 1000)
            return params
        return {'aid': str(cfg.app_id), 'app_name': 'musical_ly', 'version_code': cfg.version_code, 'version_name': cfg.app_version, 'device_platform': 'android', 'device_type': cfg.device_type, 'os_version': cfg.os_version, 'ssmix': 'a', 'device_id': self.device_id, 'iid': self.iid, 'channel': cfg.channel}

    def _get_signed(self, path: str, params: dict) -> dict:
        cfg = self._config
        last_err: Exception | None = None
        for attempt in range(cfg.retries + 1):
            if self._direct:
                host = cfg.search_host
            else:
                host = cfg.api_hosts[attempt % len(cfg.api_hosts)]
            url = host + path + '?' + urllib.parse.urlencode(params)
            if self._direct:
                headers = self._signer.sign(url=url, device_id=self.device_id, iid=self.iid)
            else:
                headers = self._signer.sign(url=url, device_id=self.device_id)
            try:
                resp = self._session.get(url, headers=headers, timeout=cfg.request_timeout_s)
            except requests.RequestException as exc:
                last_err = exc
                logger.warning('request error (attempt %d): %s', attempt, exc)
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                raise RateLimited('TikTok rate-limited this request')
            if resp.status_code != 200 or not resp.content:
                last_err = TransportError(f'HTTP {resp.status_code} len {len(resp.content)}')
                logger.warning('bad response (attempt %d): %s', attempt, last_err)
                time.sleep(0.5 * (attempt + 1))
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                last_err = exc
                continue
            status_code = data.get('status_code') if isinstance(data, dict) else None
            if status_code not in (0, None):
                message = data.get('message') or data.get('status_msg') or 'unknown'
                last_err = SoftError(message)
                logger.warning('soft error (attempt %d): %s', attempt, message)
                time.sleep(0.5 * (attempt + 1))
                continue
            # direct mode: detect risk-control empty ("hit_shark") so callers see a clear error
            if self._direct and isinstance(data, dict):
                nil = (data.get('search_nil_info') or {}).get('search_nil_item')
                items_any = (data.get('data') or []) or (data.get('search_item_list') or [])
                if nil and not items_any:
                    last_err = SoftError(f'empty search result ({nil})')
                    logger.warning('empty result (attempt %d): %s', attempt, nil)
                    time.sleep(0.5 * (attempt + 1))
                    continue
            return data
        if isinstance(last_err, (RateLimited, SoftError)):
            raise last_err
        raise TransportError(f'request failed after {cfg.retries + 1} attempts: {last_err}')
