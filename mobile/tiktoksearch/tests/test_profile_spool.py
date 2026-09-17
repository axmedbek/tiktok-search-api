"""Handle-resolution failures must correlate with the profile visit safely."""
from __future__ import annotations

import json

import pytest

from tiktoksearch.harvest_spool import (
    INVALID_UNIQUE_ID_CODE,
    PROFILE_PATH,
    STATUS_ERROR,
    STATUS_UNAVAILABLE,
    UNIQUE_ID_PARAM,
    UNIQUE_ID_PATH,
    ProfileEntry,
    ProfileSpool,
    write_profile_entry,
)


RESOLVE_URL = 'https://api.example.tiktokv.com/aweme/v1/user/uniqueid/?id=Demo.User'


class TestHandleResolverResponses:
    def test_measured_invalid_handle_is_keyed_by_request_and_survives_disk(self, tmp_path):
        entry = ProfileEntry.from_resolver_response(
            url=RESOLVE_URL, captured_at=100.0,
            body=b'{"status_code":8196,"status_msg":"Unique ID is invalid"}')
        assert UNIQUE_ID_PATH == '/aweme/v1/user/uniqueid/'
        assert UNIQUE_ID_PARAM == 'id'
        assert INVALID_UNIQUE_ID_CODE == 8196
        assert entry is not None
        assert entry.handle == 'demo.user'
        assert entry.is_unavailable and not entry.is_ok
        assert entry.status == STATUS_UNAVAILABLE
        assert entry.user_id is None and entry.username is None
        write_profile_entry(str(tmp_path), entry)

        read_back = ProfileSpool(str(tmp_path)).newest_since('DEMO.USER', after=99.0)
        assert read_back == entry
        assert read_back.is_unavailable

    @pytest.mark.parametrize('code', [8197, 2065, 'error', None, True, False, 8196.5, '8196.5'])
    def test_unknown_or_malformed_code_yields_no_entry(self, code):
        # Only the confirmed 8196 rejection is written under the handle; any
        # other code leaves the real profile reply as the only answer.
        entry = ProfileEntry.from_resolver_response(
            url=RESOLVE_URL, captured_at=100.0,
            body=json.dumps({'status_code': code, 'status_msg': 'Unknown response'}).encode())
        assert entry is None

    @pytest.mark.parametrize('body', [None, b'', b'<html>error</html>', b'[]', b'null', b'{}'])
    def test_unreadable_reply_yields_no_entry(self, body):
        # Measured 2026-09-17: an unreadable resolver body written under the
        # handle made the driver fail the visit before the profile reply landed.
        assert ProfileEntry.from_resolver_response(url=RESOLVE_URL, captured_at=100.0, body=body) is None

    @pytest.mark.parametrize('code', [0, '0'])
    def test_resolver_success_does_not_masquerade_as_a_profile(self, code):
        body = json.dumps({'status_code': code, 'user': {'uid': '123', 'unique_id': 'demo.user'}}).encode()
        assert ProfileEntry.from_resolver_response(url=RESOLVE_URL, captured_at=100.0, body=body) is None

    def test_a_late_successful_resolver_cannot_replace_a_complete_profile(self, tmp_path):
        full_profile = ProfileEntry.from_response(
            url=f'https://api.example.tiktokv.com{PROFILE_PATH}', captured_at=100.0,
            body=b'{"status_code":0,"common":{"user_profile_info":{"username":"demo.user","uid":"123"}}}')
        write_profile_entry(str(tmp_path), full_profile)
        resolver = ProfileEntry.from_resolver_response(
            url=RESOLVE_URL, captured_at=101.0, body=b'{"status_code":0,"uid":"123"}')
        assert resolver is None
        assert ProfileSpool(str(tmp_path)).newest_since('demo.user', after=99.0) == full_profile

    def test_an_unrelated_request_param_cannot_supply_the_handle(self):
        entry = ProfileEntry.from_resolver_response(
            url='https://api.example.tiktokv.com/aweme/v1/user/uniqueid/?unique_id=other&sec_user_id=sec',
            captured_at=100.0, body=b'{"status_code":8196}')
        assert entry is not None and entry.handle == ''

    def test_profile_error_is_not_assumed_to_prove_a_missing_handle(self):
        entry = ProfileEntry.from_response(
            url=f'https://api.example.tiktokv.com{PROFILE_PATH}?sec_user_id=sec',
            captured_at=100.0, body=b'{"status_code":8196}')
        assert entry.status == STATUS_ERROR and not entry.is_unavailable
