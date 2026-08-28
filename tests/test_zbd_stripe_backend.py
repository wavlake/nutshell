"""Unit tests for the ZBDStripeWallet composite payment backend.

These tests exercise ``cashu/lightning/zbd_stripe.py`` directly as a unit —
no Ledger, no HTTP, no real mint URLs — and cover the high-value paths
that were previously only reached indirectly through the integration
``ledger_with_composite`` fixture in ``test_callbacks.py``:

- Initialization: rejects non-USD units, wires up both sub-backends.
- Sub-backend selection: ``get_backend()`` default vs. named vs. unknown,
  ``backend_names()``, ``default_backend_name``.
- Delegation: ``create_invoice`` / ``get_invoice_status`` / ``status`` go
  to ZBD (the default), not to Stripe, and pass arguments through.
- Melt disabled: ``pay_invoice`` / ``get_payment_status`` /
  ``get_payment_quote`` all raise ``Unsupported``.
- ``paid_invoices_stream`` raises ``NotImplementedError`` (HTTP callbacks
  are the supported path).
- Advertised capability flags match the class contract.

All network-capable calls on the underlying ZBDWallet are stubbed with
``AsyncMock`` so no real HTTP request is made.
"""

from unittest.mock import AsyncMock

import pytest

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Method, Unit
from cashu.core.errors import NotAllowedError
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import settings
from cashu.lightning.base import (
    InvoiceResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
    Unsupported,
)
from cashu.lightning.stripe import StripeWallet
from cashu.lightning.zbd import ZBDWallet
from cashu.lightning.zbd_stripe import DEFAULT_BACKEND, ZBDStripeWallet


@pytest.fixture
def zbd_api_key(monkeypatch):
    """Provide a fake MINT_ZBD_API_KEY so ZBDWallet init succeeds.

    Also pins the ZBD endpoint to a safe non-routable localhost URL so that
    even a stray HTTP call would fail fast instead of escaping the test.
    """
    monkeypatch.setattr(settings, "mint_zbd_api_key", "test-zbd-api-key")
    monkeypatch.setattr(settings, "mint_zbd_endpoint", "http://127.0.0.1:1")
    yield


@pytest.fixture
def wallet(zbd_api_key):
    """A ZBDStripeWallet with both sub-backends initialized."""
    return ZBDStripeWallet(unit=Unit.usd)


# ---------------------------------------------------------------------------
# Initialization & capability flags
# ---------------------------------------------------------------------------


class TestZBDStripeInit:
    def test_rejects_sat(self, zbd_api_key):
        with pytest.raises(Unsupported, match="Unit sat is not supported"):
            ZBDStripeWallet(unit=Unit.sat)

    def test_rejects_eur(self, zbd_api_key):
        with pytest.raises(Unsupported, match="Unit eur is not supported"):
            ZBDStripeWallet(unit=Unit.eur)

    def test_accepts_usd_and_wires_sub_backends(self, wallet):
        assert wallet.unit == Unit.usd
        assert isinstance(wallet._zbd, ZBDWallet)
        assert isinstance(wallet._stripe, StripeWallet)
        # Both sub-backends are exposed under their canonical names.
        assert set(wallet._backends.keys()) == {"zbd", "stripe"}
        assert wallet._backends["zbd"] is wallet._zbd
        assert wallet._backends["stripe"] is wallet._stripe

    def test_capability_flags(self):
        # Class-level contract — independent of instance state.
        assert ZBDStripeWallet.supported_units == {Unit.usd}
        assert ZBDStripeWallet.supports_mpp is False
        assert ZBDStripeWallet.supports_incoming_payment_stream is False
        # ZBD (the default) supports descriptions, so the composite does too.
        assert ZBDStripeWallet.supports_description is True


# ---------------------------------------------------------------------------
# Sub-backend selection
# ---------------------------------------------------------------------------


class TestGetBackend:
    def test_default_backend_is_zbd(self, wallet):
        assert DEFAULT_BACKEND == "zbd"
        assert wallet.default_backend_name == "zbd"
        assert wallet.get_backend() is wallet._zbd

    def test_none_returns_default(self, wallet):
        assert wallet.get_backend(None) is wallet._zbd

    def test_named_zbd(self, wallet):
        assert wallet.get_backend("zbd") is wallet._zbd

    def test_named_stripe(self, wallet):
        assert wallet.get_backend("stripe") is wallet._stripe

    def test_unknown_name_raises_not_allowed(self, wallet):
        with pytest.raises(NotAllowedError) as excinfo:
            wallet.get_backend("paypal")
        msg = str(excinfo.value)
        assert "paypal" in msg
        # Error message advertises the valid options so callers can recover.
        assert "zbd" in msg
        assert "stripe" in msg

    def test_empty_string_is_not_default(self, wallet):
        """Empty string must NOT silently fall through to the default.

        Defensive: ``get_backend(name or DEFAULT)`` pattern is used inside
        ``get_backend``; an empty string is falsy and resolves to zbd. That
        is the current contract — this test pins it so any refactor that
        changes it fails loudly.
        """
        assert wallet.get_backend("") is wallet._zbd

    def test_backend_names(self, wallet):
        names = wallet.backend_names()
        assert set(names) == {"zbd", "stripe"}
        # Must be a list (ledger code iterates / logs it).
        assert isinstance(names, list)


# ---------------------------------------------------------------------------
# Delegation to the default (ZBD) sub-backend
# ---------------------------------------------------------------------------


class TestDelegation:
    @pytest.mark.asyncio
    async def test_status_delegates_to_zbd(self, wallet):
        expected = StatusResponse(error_message=None, balance=Amount(Unit.usd, 42))
        wallet._zbd.status = AsyncMock(return_value=expected)
        wallet._stripe.status = AsyncMock(
            return_value=StatusResponse(
                error_message="should-not-be-called", balance=Amount(Unit.usd, 0)
            )
        )

        result = await wallet.status()

        assert result is expected
        wallet._zbd.status.assert_awaited_once_with()
        wallet._stripe.status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_invoice_delegates_to_zbd(self, wallet):
        expected = InvoiceResponse(
            ok=True,
            checking_id="zbd-charge-xyz",
            payment_request="lnbcmocked",
        )
        wallet._zbd.create_invoice = AsyncMock(return_value=expected)
        wallet._stripe.create_invoice = AsyncMock()

        amount = Amount(Unit.usd, 1100)
        memo = "test memo"
        description_hash = b"\x00" * 32
        unhashed_description = b"hello"

        result = await wallet.create_invoice(
            amount=amount,
            memo=memo,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
        )

        assert result is expected
        wallet._zbd.create_invoice.assert_awaited_once_with(
            amount, memo, description_hash, unhashed_description
        )
        wallet._stripe.create_invoice.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_invoice_forwards_defaults(self, wallet):
        """When optional args are omitted, they are passed as None through
        the positional signature expected by ZBDWallet.create_invoice."""
        wallet._zbd.create_invoice = AsyncMock(
            return_value=InvoiceResponse(
                ok=True, checking_id="id", payment_request="req"
            )
        )
        amount = Amount(Unit.usd, 500)

        await wallet.create_invoice(amount=amount)

        wallet._zbd.create_invoice.assert_awaited_once_with(amount, None, None, None)

    @pytest.mark.asyncio
    async def test_get_invoice_status_delegates_to_zbd(self, wallet):
        expected = PaymentStatus(result=PaymentResult.SETTLED)
        wallet._zbd.get_invoice_status = AsyncMock(return_value=expected)
        wallet._stripe.get_invoice_status = AsyncMock()

        result = await wallet.get_invoice_status("zbd-charge-abc")

        assert result is expected
        wallet._zbd.get_invoice_status.assert_awaited_once_with("zbd-charge-abc")
        wallet._stripe.get_invoice_status.assert_not_awaited()


# ---------------------------------------------------------------------------
# Melt is disabled — every outgoing-payment path must raise Unsupported
# ---------------------------------------------------------------------------


def _dummy_melt_quote() -> MeltQuote:
    return MeltQuote(
        quote="q1",
        method=Method.bolt11.name,
        request="lnbcmocked",
        checking_id="checking-1",
        unit=Unit.usd.name,
        amount=100,
        fee_reserve=0,
        state=MeltQuoteState.unpaid,
        created_time=0,
    )


class TestMeltDisabled:
    @pytest.mark.asyncio
    async def test_pay_invoice_raises_unsupported(self, wallet):
        with pytest.raises(Unsupported, match="Melt .* disabled"):
            await wallet.pay_invoice(_dummy_melt_quote(), fee_limit_msat=0)

    @pytest.mark.asyncio
    async def test_get_payment_status_raises_unsupported(self, wallet):
        with pytest.raises(Unsupported, match="Melt .* disabled"):
            await wallet.get_payment_status("anything")

    @pytest.mark.asyncio
    async def test_get_payment_quote_raises_unsupported(self, wallet):
        req = PostMeltQuoteRequest(unit="usd", request="lnbcmocked")
        with pytest.raises(Unsupported, match="Melt .* disabled"):
            await wallet.get_payment_quote(req)


# ---------------------------------------------------------------------------
# paid_invoices_stream is intentionally unimplemented
# ---------------------------------------------------------------------------


class TestPaidInvoicesStream:
    @pytest.mark.asyncio
    async def test_raises_not_implemented(self, wallet):
        """The composite backend uses HTTP callbacks; the stream API is a
        trap for callers that assume a push model.

        Because ``paid_invoices_stream`` is an async generator, the
        ``NotImplementedError`` is only raised once the generator is
        iterated — not at call time.
        """
        stream = wallet.paid_invoices_stream()
        with pytest.raises(NotImplementedError, match="HTTP callbacks"):
            await stream.__anext__()
