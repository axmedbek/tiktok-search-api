from __future__ import annotations
import logging
import random
import time
import urllib.parse
from typing import Callable, Optional
import requests
from .config import ClientConfig
from .errors import RateLimited, SoftError, TransportError
from .filters import SearchKind, SearchQuery
from .mapping import flatten_user, flatten_video
from .signing import MetasecSigner
logger = logging.getLogger('tiktoksearch.client')
SEARCH_VIDEO_PATH = '/aweme/v1/general/search/single/'
SEARCH_USER_PATH = '/aweme/v1/discover/search/'
_ID_LO = 7000000000000000000
_ID_HI = 7499999999999999999

def _synth_id() -> str:
    return str(random.randint(_ID_LO, _ID_HI))

class TikTokClient:

    def __init__(self, config: ClientConfig, *, signer: Optional[MetasecSigner]=None) -> None:
        self._config = config
        self.device_id = config.device_id or _synth_id()
        self.iid = config.iid or _synth_id()
        self.proxy = config.proxy
        self._signer = signer or MetasecSigner(config)
        self._session = requests.Session()
        if config.proxy:
            self._session.proxies = {'http': config.proxy, 'https': config.proxy}

    def search(self, query: SearchQuery) -> list[dict]:
        if query.kind is SearchKind.USER:
            return self._search_users(query)
        return self._search_videos(query)

    def _search_videos(self, query: SearchQuery) -> list[dict]:
        filter_params = query.filters.to_query_params()

        def build(offset: int, count: int) -> dict:
            params = {'keyword': query.keyword, 'count': str(count), 'offset': str(offset), 'search_source': 'normal_search'}
            params.update(filter_params)
            return params
        return self._paginate(path=SEARCH_VIDEO_PATH, build_params=build, items_key='data', unwrap=lambda item: item.get('aweme_info') or item.get('aweme') or (item if item.get('aweme_id') else None), flatten=flatten_video, cursor_key='cursor', source_term=query.source_term, limit=query.limit)

    def _search_users(self, query: SearchQuery) -> list[dict]:

        def build(offset: int, count: int) -> dict:
            return {'keyword': query.term, 'count': str(count), 'cursor': str(offset), 'type': '1', 'search_source': 'normal_search'}
        return self._paginate(path=SEARCH_USER_PATH, build_params=build, items_key='user_list', unwrap=lambda item: item.get('user_info') or item, flatten=flatten_user, cursor_key='cursor', source_term=query.source_term, limit=query.limit)

    def _paginate(self, *, path: str, build_params: Callable[[int, int], dict], items_key: str, unwrap: Callable[[dict], Optional[dict]], flatten: Callable[[dict, str], Optional[dict]], cursor_key: str, source_term: str, limit: int) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        while len(out) < limit:
            params = self._common_params()
            params.update(build_params(offset, min(20, limit - len(out))))
            data = self._get_signed(path, params)
            raw_items = data.get(items_key) or []
            if not raw_items:
                break
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
                if len(out) >= limit:
                    break
            if not data.get('has_more'):
                break
            offset = data.get(cursor_key, offset + len(raw_items))
        return out

    def _common_params(self) -> dict:
        cfg = self._config
        return {'aid': str(cfg.app_id), 'app_name': 'musical_ly', 'version_code': cfg.version_code, 'version_name': cfg.app_version, 'device_platform': 'android', 'device_type': cfg.device_type, 'os_version': cfg.os_version, 'ssmix': 'a', 'device_id': self.device_id, 'iid': self.iid, 'channel': cfg.channel}

    def _get_signed(self, path: str, params: dict) -> dict:
        cfg = self._config
        last_err: Exception | None = None
        for attempt in range(cfg.retries + 1):
            host = cfg.api_hosts[attempt % len(cfg.api_hosts)]
            url = host + path + '?' + urllib.parse.urlencode(params)
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
            return data
        if isinstance(last_err, (RateLimited, SoftError)):
            raise last_err
        raise TransportError(f'request failed after {cfg.retries + 1} attempts: {last_err}')
