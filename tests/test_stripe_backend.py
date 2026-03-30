"""Unit tests for Stripe payment backend.

Tests cover:
- Initialization with unit validation
- Invoice creation returning UUID checking_id and stripe-prefixed payment_request
- Status returning zero balance with no error
- Melt operations raising Unsupported exceptions
- Invoice status always returning PENDING
- supports_incoming_payment_stream is False (uses HTTP callbacks)
"""

import uuid
from unittest.mock import MagicMock

import pytest

from cashu.core.base import Amount, Unit
from cashu.lightning.base import PaymentResult, Unsupported
from cashu.lightning.stripe import StripeWallet


class TestStripeWallet:
    """Test suite for StripeWallet payment backend."""

    @pytest.fixture
    def stripe_wallet(self):
        """Create a StripeWallet instance."""
        return StripeWallet(unit=Unit.usd)

    def test_init_rejects_non_usd(self):
        """Test that non-USD units raise Unsupported exception."""
        with pytest.raises(Unsupported, match="Unit sat is not supported"):
            StripeWallet(unit=Unit.sat)

    def test_init_rejects_eur(self):
        """Test that EUR unit raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Unit eur is not supported"):
            StripeWallet(unit=Unit.eur)

    @pytest.mark.asyncio
    async def test_create_invoice_returns_uuid(self, stripe_wallet):
        """Test that create_invoice returns a valid UUID checking_id and stripe-prefixed payment_request."""
        result = await stripe_wallet.create_invoice(
            amount=Amount(Unit.usd, 100), memo="Test invoice"
        )

        assert result.ok is True
        assert result.error_message is None

        # Verify checking_id is a valid UUID
        parsed = uuid.UUID(result.checking_id)
        assert str(parsed) == result.checking_id

        # payment_request should be stripe-prefixed and different from checking_id
        assert result.payment_request == f"stripe:{result.checking_id}"
        assert result.payment_request != result.checking_id

    @pytest.mark.asyncio
    async def test_create_invoice_unique_ids(self, stripe_wallet):
        """Test that each create_invoice call returns a unique UUID."""
        result1 = await stripe_wallet.create_invoice(
            amount=Amount(Unit.usd, 100), memo="Invoice 1"
        )
        result2 = await stripe_wallet.create_invoice(
            amount=Amount(Unit.usd, 100), memo="Invoice 2"
        )

        assert result1.checking_id != result2.checking_id

    @pytest.mark.asyncio
    async def test_status_returns_ok(self, stripe_wallet):
        """Test that status returns zero balance with no error."""
        result = await stripe_wallet.status()

        assert result.error_message is None
        assert result.balance.amount == 0
        assert result.balance.unit == Unit.usd

    @pytest.mark.asyncio
    async def test_pay_invoice_raises_unsupported(self, stripe_wallet):
        """Test that pay_invoice raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await stripe_wallet.pay_invoice(MagicMock(), 1000)

    @pytest.mark.asyncio
    async def test_get_payment_status_raises_unsupported(self, stripe_wallet):
        """Test that get_payment_status raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await stripe_wallet.get_payment_status("payment_123")

    @pytest.mark.asyncio
    async def test_get_payment_quote_raises_unsupported(self, stripe_wallet):
        """Test that get_payment_quote raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await stripe_wallet.get_payment_quote(MagicMock())

    @pytest.mark.asyncio
    async def test_get_invoice_status_returns_pending(self, stripe_wallet):
        """Test that get_invoice_status always returns PENDING."""
        result = await stripe_wallet.get_invoice_status("some-checking-id")

        assert result.result == PaymentResult.PENDING

    def test_supported_units_usd_only(self, stripe_wallet):
        """Test that only USD is supported."""
        assert Unit.usd in stripe_wallet.supported_units
        assert Unit.sat not in stripe_wallet.supported_units
        assert Unit.eur not in stripe_wallet.supported_units

    def test_supports_incoming_payment_stream(self, stripe_wallet):
        """Test that incoming payment stream is disabled (uses HTTP callbacks)."""
        assert stripe_wallet.supports_incoming_payment_stream is False

    def test_mpp_not_supported(self, stripe_wallet):
        """Test that multi-path payments are not supported."""
        assert stripe_wallet.supports_mpp is False
