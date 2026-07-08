from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

def to_int(value: Any) -> int | None:
    try:
        if value is None or value == '':
            return None
        return int(value)
    except (TypeError, ValueError):
        return None

def _iso_utc(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

def _collect_hashtags(aweme: dict) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for challenge in aweme.get('cha_list') or []:
        name = challenge.get('cha_name') if isinstance(challenge, dict) else None
        if name and name.lower() not in seen:
            seen.add(name.lower())
            tags.append(name)
    for extra in aweme.get('text_extra') or []:
        name = extra.get('hashtag_name') if isinstance(extra, dict) else None
        if name and name.lower() not in seen:
            seen.add(name.lower())
            tags.append(name)
    return tags

def flatten_video(aweme: dict, source_term: str) -> dict | None:
    aweme_id = aweme.get('aweme_id')
    if aweme_id is None:
        return None
    author = aweme.get('author') or {}
    stats = aweme.get('statistics') or {}
    music = aweme.get('added_sound_music_info') or aweme.get('music') or {}
    uid = author.get('uid')
    return {'id': str(aweme_id), 'description': aweme.get('desc'), 'create_time': _iso_utc(to_int(aweme.get('create_time'))), 'author_username': author.get('unique_id') or author.get('nickname'), 'author_id': str(uid) if uid is not None else None, 'region_code': aweme.get('region') or author.get('region'), 'view_count': to_int(stats.get('play_count')), 'like_count': to_int(stats.get('digg_count')), 'comment_count': to_int(stats.get('comment_count')), 'share_count': to_int(stats.get('share_count')), 'hashtags': _collect_hashtags(aweme), 'music_id': str(music.get('id')) if music.get('id') is not None else None, 'music_title': music.get('title'), 'duration': to_int((aweme.get('video') or {}).get('duration')) or to_int(aweme.get('duration')), 'source_term': source_term}

def flatten_user(user: dict, source_term: str) -> dict | None:
    uid = user.get('uid')
    username = user.get('unique_id')
    if uid is None and (not username):
        return None
    uid_str = str(uid) if uid is not None else None
    return {'type': 'user', 'id': uid_str, 'username': username, 'display_name': user.get('nickname'), 'follower_count': to_int(user.get('follower_count')), 'following_count': to_int(user.get('following_count')), 'aweme_count': to_int(user.get('aweme_count')), 'signature': user.get('signature'), 'region_code': user.get('region'), 'verified': bool(user.get('custom_verify') or user.get('enterprise_verify_reason')), 'user_id': uid_str, 'source_term': source_term}
