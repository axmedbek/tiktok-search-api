"""Unit tests for client.py's signer wiring: which mode takes the direct path,
and how narrow the paid fallback is.

The fallback is the part of this Epic that carries anti-block risk, so the
assertions are on the paid-signer LEDGER (`rapid_ledger`, one entry per
`RapidSigner` construction) rather than on which exception came out. Swapping
signers because a VALID signature was answered emptily is precisely the
misdiagnosis `.claude/rules/lessons/anti-block.md` is organised against, and a
regression there is silent and expensive.

The local signer runs its real in-process crypto except where a test needs it to
FAIL, in which case `MetasecSpy` replaces the vendored object. No real
`RapidSigner` is ever constructed and no socket is ever touched.

Run:  cd mobile && ../.venv/bin/python -m pytest tiktoksearch/tests -q
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Make the package importable when run from the repo without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conftest import (  # noqa: E402
    STUB_SIGNER_KEY,
    FakeTransport,
    MetasecSpy,
    empty_reply,
    local_config,
    reply,
)

from tiktoksearch.client import TikTokClient  # noqa: E402
from tiktoksearch.config import (  # noqa: E402
    SIGNER_LEGACY,
    SIGNER_LOCAL,
    SIGNER_RAPID,
    ClientConfig,
)
from tiktoksearch.errors import RateLimited, SoftError, TransportError  # noqa: E402
from tiktoksearch.filters import SearchQuery  # noqa: E402
from tiktoksearch.signing import MetasecSigner  # noqa: E402
from tiktoksearch.tiktok_signer import MetasecBaseException  # noqa: E402

CLIENT_LOGGER = 'tiktoksearch.client'
CLIENT_SOURCE = Path(__file__).resolve().parents[1] / 'client.py'
URL = 'https://search19-normal-alisg.tiktokv.com/aweme/v1/search/item/?keyword=ocean'
# The direct path fetches 10 per page; the cold legacy path asked for 20.
DIRECT_PAGE_COUNT = '10'
LEGACY_PAGE_COUNT = '20'
NO_FALLBACK_LOG = 'no RapidAPI fallback is configured'


def _local(**over) -> TikTokClient:
    """A `signer: local` client with a synthetic warm identity."""
    return TikTokClient(local_config(**{'retries': 1, **over}))


def _breaking_local(**over) -> tuple[TikTokClient, MetasecSpy]:
    """A local client whose vendored signer raises on every signing call."""
    client = _local(**over)
    spy = MetasecSpy(raises=MetasecBaseException('boom'))
    client._signer._metasec = spy
    return client, spy


def _query() -> SearchQuery:
    return SearchQuery(kind='keyword', term='ocean', limit=10)


class TestDirectIsModeDriven:
    """`_direct` used to be `bool(config.rapidapi_key)`, which gave the paid
    signer a monopoly on the working path AND silently disabled hit_shark
    detection for a keyless profile."""

    def test_local_and_rapid_take_the_direct_path(self):
        assert _local()._direct is True
        assert TikTokClient(
            ClientConfig(signer=SIGNER_RAPID, rapidapi_key=STUB_SIGNER_KEY)
        )._direct is True

    def test_legacy_stays_on_the_cold_path_even_with_a_key(self):
        client = TikTokClient(
            local_config(signer=SIGNER_LEGACY, rapidapi_key=STUB_SIGNER_KEY)
        )
        assert client._mode == SIGNER_LEGACY
        assert client._direct is False

    def test_a_keyless_local_client_is_direct(self):
        client = _local(rapidapi_key=None)
        assert client._mode == SIGNER_LOCAL
        assert not client._config.rapidapi_key
        assert client._direct is True

    def test_each_mode_holds_its_own_signer(self):
        local, rapid = _local(), TikTokClient(
            ClientConfig(signer=SIGNER_RAPID, rapidapi_key=STUB_SIGNER_KEY)
        )
        legacy = TikTokClient(local_config(signer=SIGNER_LEGACY))
        assert isinstance(local._signer, MetasecSigner) and local._signer._v46 is True
        assert isinstance(legacy._signer, MetasecSigner) and legacy._signer._v46 is False
        assert not isinstance(rapid._signer, MetasecSigner)

    def test_no_client_starts_with_a_fallback_signer(self):
        # `_fallback` is built lazily and only by `_rapid_fallback`.
        assert _local(rapidapi_key=STUB_SIGNER_KEY)._fallback is None


class TestKeylessLocalStillDetectsRiskControl:
    """Anti-block invariant (a) must hold with no key configured at all: the
    silent-200 shape was a configuration failure wearing risk-control's
    clothes, and it has to stay structurally unreachable."""

    def test_a_shadow_block_raises_softerror_never_a_silent_empty(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: empty_reply())
        with pytest.raises(SoftError):
            _local(rapidapi_key=None).search(_query())
        assert transport.calls, 'nothing was requested, so nothing was classified'

    def test_a_keyless_local_search_signs_and_requests_for_real(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: reply([1, 2], cursor=10, has_more=False))
        page = _local(rapidapi_key=None).search(_query())
        assert [r['id'] for r in page.records] == ['1', '2']
        assert all(call['headers']['x-argus'] for call in transport.calls)

    def test_a_keyless_local_client_uses_the_direct_page_count(
        self, transport: FakeTransport
    ):
        transport.script(lambda call: reply([1], cursor=10, has_more=False))
        _local(rapidapi_key=None).search(_query())
        assert {c['params']['count'] for c in transport.calls} == {DIRECT_PAGE_COUNT}

    def test_legacy_keeps_the_cold_page_count_and_no_hit_shark_detection(
        self, transport: FakeTransport
    ):
        # Unchanged cold path: an empty reply is empty by design, not a 502.
        transport.script(lambda call: empty_reply())
        page = TikTokClient(
            ClientConfig(signer=SIGNER_LEGACY, retries=1, device_id='FAKE-DEV-1')
        ).search(_query())
        assert page.records == []
        assert {c['params']['count'] for c in transport.calls} == {LEGACY_PAGE_COUNT}


class TestFallbackFiresOnSigningFailure:
    def test_a_signer_transport_error_is_re_signed_by_the_paid_signer(
        self, rapid_ledger: list
    ):
        client, _ = _breaking_local(rapidapi_key=STUB_SIGNER_KEY)
        headers = client._sign(URL)
        assert headers['x-signed-by'] == 'rapid-fallback'
        assert len(rapid_ledger) == 1

    def test_the_paid_signer_is_constructed_once_per_client(self, rapid_ledger: list):
        client, _ = _breaking_local(rapidapi_key=STUB_SIGNER_KEY)
        for _ in range(3):
            client._sign(URL)
        assert len(rapid_ledger) == 1
        assert client._fallback is rapid_ledger[0]

    def test_the_local_signer_is_still_tried_first_after_a_fallback(
        self, rapid_ledger: list
    ):
        # So a recovered local path stops spending quota by itself.
        client, spy = _breaking_local(rapidapi_key=STUB_SIGNER_KEY)
        for _ in range(3):
            client._sign(URL)
        assert spy.calls == 3

    def test_the_switch_is_logged_once_at_warning_without_key_material(
        self, rapid_ledger: list, caplog: pytest.LogCaptureFixture
    ):
        client, _ = _breaking_local(rapidapi_key=STUB_SIGNER_KEY)
        with caplog.at_level(logging.WARNING, logger=CLIENT_LOGGER):
            for _ in range(3):
                client._sign(URL)
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert 'falling back to the RapidAPI signer' in warnings[0]
        assert STUB_SIGNER_KEY not in warnings[0]


class TestFallbackRefusesEmptyResults:
    """A stale sign key raises NOTHING — it produces a well-formed signature
    that risk-control answers with an empty `data[]`. Falling back there would
    pay for a signature to answer an identity problem."""

    def test_a_full_retry_run_of_empties_never_builds_a_paid_signer(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        transport.script(lambda call: empty_reply())
        client = _local(rapidapi_key=STUB_SIGNER_KEY, retries=2)
        with pytest.raises(SoftError):
            client.search(_query())
        assert transport.calls, 'no request was made, so no empty was classified'
        assert rapid_ledger == []
        assert client._fallback is None

    def test_a_risk_control_nil_never_builds_a_paid_signer(
        self, transport: FakeTransport, rapid_ledger: list
    ):
        transport.script(lambda call: empty_reply(nil='hit_shark'))
        client = _local(rapidapi_key=STUB_SIGNER_KEY, retries=2)
        with pytest.raises(SoftError):
            client.search(_query())
        assert rapid_ledger == []

    def test_softerror_is_structurally_invisible_to_the_fallback(self):
        # The `except TransportError` in `_sign` cannot see these, whatever a
        # future edit does to the handler body.
        assert not issubclass(SoftError, TransportError)
        assert not issubclass(RateLimited, TransportError)

    def test_the_fallback_has_exactly_one_call_site(self):
        lines = CLIENT_SOURCE.read_text(encoding='utf-8').splitlines()
        calls = [ln for ln in lines if '_rapid_fallback(' in ln and 'def ' not in ln]
        assert len(calls) == 1
        assert '_sign' not in calls[0]  # reached from _sign's handler, not elsewhere


class TestFallbackNeedsAKey:
    def test_a_keyless_local_failure_is_re_raised(self, rapid_ledger: list):
        client, _ = _breaking_local(rapidapi_key=None)
        with pytest.raises(TransportError) as excinfo:
            client._sign(URL)
        assert MetasecBaseException.__name__ in str(excinfo.value)
        assert rapid_ledger == []
        assert client._fallback is None

    def test_a_keyless_local_failure_is_logged_at_error(
        self, rapid_ledger: list, caplog: pytest.LogCaptureFixture
    ):
        client, _ = _breaking_local(rapidapi_key=None)
        with caplog.at_level(logging.ERROR, logger=CLIENT_LOGGER):
            with pytest.raises(TransportError):
                client._sign(URL)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any(NO_FALLBACK_LOG in message for message in errors)

    def test_rapid_mode_never_falls_back_to_a_second_signer(self, rapid_ledger: list):
        client = TikTokClient(
            ClientConfig(signer=SIGNER_RAPID, rapidapi_key=STUB_SIGNER_KEY)
        )
        assert len(rapid_ledger) == 1, 'the primary signer, built at construction'

        def _fail(**kwargs):
            raise TransportError('rapid signing failed')

        client._signer.sign = _fail
        with pytest.raises(TransportError):
            client._sign(URL)
        assert len(rapid_ledger) == 1  # no second, nowhere narrower to go
        assert client._fallback is None
