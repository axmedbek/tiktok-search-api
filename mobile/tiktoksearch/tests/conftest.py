"""Shared scaffolding for the page_token and local-signer regression nets.

Four things live here, and nothing else:

* **The no-network tripwire.** `requests.adapters.HTTPAdapter.send` is replaced
  for the whole session, so ANY test that reaches a real socket — TikTok or the
  paid RapidAPI signer — fails loudly instead of spending the developer's
  money. Every test also asserts, on the way out, that it attempted nothing.
* **The signer stub.** `client.RapidSigner` is swapped for a fake class, so no
  real `RapidSigner` is ever constructed and `sign()` never leaves the process.
  The retry backoff inside `_get_signed` is nulled at the same time: retries
  are behaviour under test, sleeping for them is not.
* **Canned TikTok replies + a stdlib ASGI driver.** `starlette.testclient`
  needs `httpx`, which is not a project dependency and is not worth adding for
  a test; the app is an ASGI callable, so it can be driven directly.
* **Local-signer seams.** `MetasecSpy` stands in for the vendored `Metasec`
  object so the params handed to it are observable, and `rapid_ledger` counts
  paid-signer CONSTRUCTIONS. The local signer's crypto is in-process, so tests
  may run it for REAL — that is a signature, not a request, and the tripwire
  above still forbids anything leaving the process.

Test modules import the helpers from here (`from conftest import reply`).
Never reads `mobile/identities.json`: identity fixtures are synthetic temp
files with fake device ids and a fake cookie.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.parse
from pathlib import Path

import pytest
import requests
from requests.adapters import HTTPAdapter

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tiktoksearch import client as client_module  # noqa: E402
from tiktoksearch.config import SIGNER_LOCAL, ClientConfig  # noqa: E402

# A key-shaped placeholder: it only has to be truthy for `_direct` mode and to
# get past RapidSigner's constructor guard. The signer class is stubbed out, so
# it is never sent anywhere.
STUB_SIGNER_KEY = 'stub-not-a-real-key'
# Synthetic warm identities. Fake ids, fake cookie — the shape only has to
# satisfy IdentityStore, never TikTok.
FAKE_COOKIE = 'sessionid=FAKE-COOKIE-1'
FAKE_TOKEN = 'FAKE-X-TT-TOKEN'
# Stands in for an identity's CAPTURED User-Agent. Synthetic, and deliberately
# unlike the UA the signer builds itself, so the two are distinguishable.
FAKE_UA = ('com.zhiliaoapp.musically/2024604020 (Linux; U; Android 13; en; '
           'FAKE-DEV; Build/FAKE.000000.000)')


# --------------------------------------------------------------- no network
_NETWORK_ATTEMPTS: list[str] = []


@pytest.fixture(scope='session', autouse=True)
def _forbid_real_network():
    """Hard tripwire under the whole suite. A unit test that signs or searches
    for real is not a slow test, it is a bill and a burned identity."""
    original = HTTPAdapter.send

    def _tripwire(self, request, **kwargs):  # noqa: ANN001
        _NETWORK_ATTEMPTS.append(getattr(request, 'url', '?'))
        raise AssertionError(
            f'a test attempted a REAL HTTP request: {getattr(request, "url", "?")} — '
            'stub requests.Session.get / TikTokClient._get_signed instead'
        )

    HTTPAdapter.send = _tripwire
    try:
        yield
    finally:
        HTTPAdapter.send = original


@pytest.fixture(autouse=True)
def _assert_no_network_attempted():
    """Attribute a tripwire hit to the test that caused it, even if that test
    swallowed the AssertionError (`_get_signed` catches broadly on retry)."""
    before = len(_NETWORK_ATTEMPTS)
    yield
    assert len(_NETWORK_ATTEMPTS) == before, (
        f'network attempted: {_NETWORK_ATTEMPTS[before:]}'
    )


class FakeSigner:
    """Stands in for RapidSigner: same constructor shape, no HTTP, no key use."""

    def __init__(self, config) -> None:  # noqa: ANN001
        self.config = config

    def sign(self, **kwargs) -> dict[str, str]:
        return {'x-argus': 'STUB', 'x-gorgon': 'STUB', 'x-ladon': 'STUB',
                'x-khronos': '0'}

    def user_agent(self) -> str:
        return 'stub-ua'


@pytest.fixture(autouse=True)
def _stub_signer_and_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real RapidSigner is ever constructed, and retries do not sleep."""
    monkeypatch.setattr(client_module, 'RapidSigner', FakeSigner)
    monkeypatch.setattr(client_module.time, 'sleep', lambda *_a, **_k: None)


# ------------------------------------------------------- local-signer seams
def local_config(**over) -> ClientConfig:
    """A warm `signer: local` config, built from synthetic identity material.

    Never derived from `mobile/identities.json`: fake device ids, a fake cookie
    and a fake token. Pass `signer=SIGNER_LEGACY` to get the SAME warm material
    on a legacy config — that is how the `_v46` gate gets tested on a config
    that does carry an identity."""
    base = dict(signer=SIGNER_LOCAL, cookie=FAKE_COOKIE, x_tt_token=FAKE_TOKEN,
                user_agent=FAKE_UA, device_id='FAKE-DEV-1', iid='FAKE-IID-1',
                device_query={'device_id': 'FAKE-DEV-1', 'iid': 'FAKE-IID-1'})
    base.update(over)
    return ClientConfig(**base)


class MetasecSpy:
    """Stands in for the vendored `Metasec` object a `MetasecSigner` holds.

    Assigned over `signer._metasec`, it records the kwargs handed to `sign` —
    which is the only place the v46-vs-v32 param mapping is observable, and
    feeding v32 was the original bug — and optionally raises instead of
    signing, to drive `_SIGNING_FAILURES`."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.seen: dict = {}
        self.calls = 0
        self.raises = raises

    def sign(self, **kwargs) -> dict:
        self.calls += 1
        self.seen = dict(kwargs)
        if self.raises is not None:
            raise self.raises
        return {'x-argus': 'A', 'x-ladon': 'L', 'x-gorgon': 'G', 'x-khronos': 1}


@pytest.fixture
def rapid_ledger(monkeypatch: pytest.MonkeyPatch) -> list:
    """Ledger of paid-signer CONSTRUCTIONS, one entry per `RapidSigner()`.

    The narrow fallback is a money question, so the assertion has to be on the
    receipt (0 constructions, or exactly 1) and not merely on which exception
    came out. Replaces the autouse `FakeSigner`, so still nothing is signed for
    real; headers it returns are tagged so a fallback signature is traceable."""
    built: list = []

    class CountingRapid(FakeSigner):
        def __init__(self, config) -> None:  # noqa: ANN001
            super().__init__(config)
            built.append(self)

        def sign(self, **kwargs) -> dict[str, str]:
            return {**super().sign(**kwargs), 'x-signed-by': 'rapid-fallback'}

    monkeypatch.setattr(client_module, 'RapidSigner', CountingRapid)
    return built


# ---------------------------------------------------- canned TikTok replies
def videos(ids, key: str = 'data') -> dict:
    """The item list of a video search reply, under `key`."""
    return {key: [{'aweme_info': {'aweme_id': str(i),
                                  'author': {'uid': '9', 'unique_id': 'u'},
                                  'statistics': {}}} for i in ids]}


def users(ids) -> dict:
    return {'user_list': [{'user_info': {'uid': str(i), 'unique_id': f'u{i}',
                                         'follower_count': 1}} for i in ids]}


def reply(ids, *, cursor: int | None, has_more: bool, key: str = 'data',
          sid: str | None = 'SID', nil: str | None = None) -> dict:
    """A successful TikTok search reply carrying `ids`.

    `cursor=None` omits the key entirely (the `_as_cursor` fallback path);
    `nil` adds a `search_nil_info` block alongside the records."""
    out: dict = {'status_code': 0, 'has_more': has_more}
    if cursor is not None:
        out['cursor'] = cursor
    if sid is not None:
        out['log_pb'] = {'impr_id': sid}
    out.update(videos(ids, key))
    if nil:
        out['search_nil_info'] = {'search_nil_item': nil}
    return out


def empty_reply(*, nil: str | None = None, has_more: bool = False,
                cursor: int = 0, key: str = 'data', sid: str | None = 'SID') -> dict:
    """An empty reply: a session tail, or risk-control, depending on `nil` and
    on whether the request that produced it carried a search_id."""
    out: dict = {'status_code': 0, 'has_more': has_more, 'cursor': cursor, key: []}
    if sid is not None:
        out['log_pb'] = {'impr_id': sid}
    if nil:
        out['search_nil_info'] = {'search_nil_item': nil}
    return out


class FakeResponse:
    """What the stubbed `Session.get` hands back. A non-200 status or empty
    body drives the TransportError branch of `_get_signed`."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.content = json.dumps(payload).encode() if payload else b''

    def json(self) -> dict:
        return self._payload


class FakeTransport:
    """Records every signed GET and answers it from a scripted handler.

    Installed over `requests.Session.get` as a plain (non-descriptor) callable,
    so it receives the call WITHOUT `self`. `calls` is the paid-sign ledger:
    one entry per signed request the code actually made."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.handler = lambda call: empty_reply()

    def script(self, handler) -> None:  # noqa: ANN001
        self.handler = handler

    def __call__(self, url, headers=None, timeout=None):  # noqa: ANN001
        parts = urllib.parse.urlsplit(url)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        call = {'path': parts.path, 'params': params,
                'device': params.get('device_id'),
                'offset': params.get('offset') or params.get('cursor'),
                'sid': params.get('search_id'), 'headers': dict(headers or {})}
        self.calls.append(call)
        out = self.handler(call)
        return out if isinstance(out, FakeResponse) else FakeResponse(out)

    @property
    def paths(self) -> list[str]:
        return [c['path'] for c in self.calls]

    @property
    def offsets(self) -> list[str | None]:
        return [c['offset'] for c in self.calls]

    @property
    def sids(self) -> list[str | None]:
        return [c['sid'] for c in self.calls]


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """The only seam a search reaches. Nothing below it exists in a test."""
    fake = FakeTransport()
    monkeypatch.setattr(requests.Session, 'get', fake)
    return fake


# ------------------------------------------------------- synthetic identities
def write_identities(path, entries: list[dict], *, stamp: int) -> None:
    """Write a synthetic identities file and stamp its mtime, so IdentityStore's
    mtime-based hot reload fires deterministically."""
    path.write_text(json.dumps(entries), encoding='utf-8')
    os.utime(path, (stamp, stamp))


def identity(device_id: str, *, cookie: str = FAKE_COOKIE,
             token: str = FAKE_TOKEN) -> dict:
    return {'device_id': device_id, 'iid': f'IID-{device_id}',
            'cookie': cookie, 'x_tt_token': token,
            'device_query': {'device_id': device_id, 'iid': f'IID-{device_id}'}}


def write_config(path, *, identities_name: str = 'ids.json', **extra) -> None:
    lines = [f"rapidapi_key: '{STUB_SIGNER_KEY}'",
             f"identities_path: '{identities_name}'",
             'retries: 2', 'acquire_timeout_s: 1',
             'daily_request_cap_per_device: 300',
             'max_results_per_search: 60']
    lines += [f'{key}: {value}' for key, value in extra.items()]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ------------------------------------------------------------- ASGI driver
class AsgiClient:
    """Minimal ASGI/1.1 driver for the FastAPI app: runs the lifespan (which is
    what builds the pool) and POSTs JSON. No httpx, no TestClient."""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app
        self._ctx = None

    async def __aenter__(self) -> 'AsgiClient':
        self._ctx = self.app.router.lifespan_context(self.app)
        await self._ctx.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._ctx.__aexit__(*exc)

    async def post(self, path: str, payload: dict) -> tuple[int, dict]:
        raw = json.dumps(payload).encode()
        scope = {
            'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
            'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
            'path': path, 'raw_path': path.encode(), 'query_string': b'',
            'root_path': '', 'client': ('127.0.0.1', 5555), 'server': ('test', 80),
            'headers': [(b'host', b'test'), (b'content-type', b'application/json'),
                        (b'content-length', str(len(raw)).encode())],
        }
        pending = [{'type': 'http.request', 'body': raw, 'more_body': False}]
        received: list[dict] = []

        async def receive() -> dict:
            return pending.pop(0) if pending else {'type': 'http.disconnect'}

        async def send(message: dict) -> None:
            received.append(message)

        await self.app(scope, receive, send)
        status = next(m['status'] for m in received
                      if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in received
                        if m['type'] == 'http.response.body')
        return status, (json.loads(body) if body else {})


def drive(coro):
    """Run one ASGI coroutine from a synchronous test (no pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
