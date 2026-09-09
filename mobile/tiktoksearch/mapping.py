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
    # `create_time` is a presentation field, so an unrenderable timestamp degrades
    # to None like every other field here instead of raising. Raising escapes
    # flatten_video, pool.run_call and _domain_errors alike, so an absurd upstream
    # value became an HTTP 500 -- or, once wrapped anywhere upstream, a 502 that
    # reads exactly like risk-control and gets triaged as a phantom hit_shark.
    # fromtimestamp rejects anything outside datetime's range (and NaN) with
    # ValueError, anything beyond the platform's time_t with OverflowError, and
    # signals a failing platform gmtime as OSError. A falsy ts stays unknown.
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None

def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None

def _flag(value: Any) -> bool:
    # TikTok serves numeric 0/1 flags that a live reply may render as the strings
    # '0'/'1', and bare bool('0') is True -- which would report a PUBLIC account as
    # private, silently and invertedly. Numbers are compared, not tested for truth.
    # A NON-numeric value has no number to compare, so it falls back to plain
    # truthiness -- which for `secret` errs toward private=True. Deliberate: an
    # unparseable flag is better shown as private than leaked as public.
    number = to_int(value)
    return bool(number) if number is not None else bool(value)

def _verified(user: dict) -> bool:
    # Unlike the numeric flags above, these are string-or-absent fields where any
    # non-empty string IS the "verified" semantic, so truthiness is correct here.
    return bool(user.get('custom_verify') or user.get('enterprise_verify_reason'))

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

_AVATAR_KEYS = ('avatar_larger', 'avatar_medium', 'avatar_thumb')

def _avatar_url(user: dict) -> str | None:
    for key in _AVATAR_KEYS:
        avatar = user.get(key)
        urls = avatar.get('url_list') if isinstance(avatar, dict) else None
        for url in urls or []:
            if url:
                return str(url)
    return None

def flatten_video(aweme: dict, source_term: str) -> dict | None:
    aweme_id = aweme.get('aweme_id')
    if aweme_id is None:
        return None
    author = aweme.get('author') or {}
    stats = aweme.get('statistics') or {}
    music = aweme.get('added_sound_music_info') or aweme.get('music') or {}
    # `author_username` is a DISPLAY field and stays one: it falls back to the
    # user-settable, non-unique `nickname`, so two different accounts can carry
    # the same value. `author_unique_id` is the IDENTITY field — the raw handle,
    # with NO nickname fallback and None when the author has none — and is the
    # only one of the two that may be compared to decide whose video this is
    # (see `api/app.py._authored_by`). Matching on the display field lets an
    # account whose nickname equals the wanted handle be attributed to it, and
    # its ids reported as that handle's: a wrong answer dressed as a right one,
    # the same failure `client._match_user_node` refuses for the resolve.
    return {'id': str(aweme_id), 'description': aweme.get('desc'), 'create_time': _iso_utc(to_int(aweme.get('create_time'))), 'author_username': author.get('unique_id') or author.get('nickname'), 'author_unique_id': _str_or_none(author.get('unique_id')), 'author_id': _str_or_none(author.get('uid')), 'author_sec_uid': _str_or_none(author.get('sec_uid')), 'region_code': aweme.get('region') or author.get('region'), 'view_count': to_int(stats.get('play_count')), 'like_count': to_int(stats.get('digg_count')), 'comment_count': to_int(stats.get('comment_count')), 'share_count': to_int(stats.get('share_count')), 'hashtags': _collect_hashtags(aweme), 'music_id': _str_or_none(music.get('id')), 'music_title': music.get('title'), 'duration': to_int((aweme.get('video') or {}).get('duration')) or to_int(aweme.get('duration')), 'source_term': source_term}

def flatten_user(user: dict, source_term: str) -> dict | None:
    uid = user.get('uid')
    username = user.get('unique_id')
    if uid is None and (not username):
        return None
    uid_str = _str_or_none(uid)
    return {'type': 'user', 'id': uid_str, 'username': username, 'display_name': user.get('nickname'), 'follower_count': to_int(user.get('follower_count')), 'following_count': to_int(user.get('following_count')), 'aweme_count': to_int(user.get('aweme_count')), 'signature': user.get('signature'), 'region_code': user.get('region'), 'verified': _verified(user), 'user_id': uid_str, 'sec_uid': _str_or_none(user.get('sec_uid')), 'source_term': source_term}

def flatten_profile(user: dict) -> dict | None:
    uid = user.get('uid')
    if uid is None:
        return None
    # heart_count is TikTok's `total_favorited` (likes RECEIVED); `favoriting_count` is likes the user GAVE.
    return {'username': user.get('unique_id'), 'user_id': str(uid), 'sec_uid': _str_or_none(user.get('sec_uid')), 'display_name': user.get('nickname'), 'signature': user.get('signature'), 'follower_count': to_int(user.get('follower_count')), 'following_count': to_int(user.get('following_count')), 'aweme_count': to_int(user.get('aweme_count')), 'heart_count': to_int(user.get('total_favorited')), 'region_code': user.get('region'), 'verified': _verified(user), 'private': _flag(user.get('secret')), 'avatar_url': _avatar_url(user)}
