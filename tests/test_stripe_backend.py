"""Unit tests for Stripe payment backend.

Tests cover:
- Initialization with required settings (mint_redis_url, unit validation)
- Invoice creation returning UUID format
- Status returning zero balance
- Melt operations raising Unsupported exceptions
- Invoice status always returning PENDING
- Redis pub/sub stream for paid invoices
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cashu.core.base import Amount, Unit
from cashu.lightning.base import PaymentResult, Unsupported


class TestStripeWallet:
    """Test suite for StripeWallet payment backend."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings for Stripe backend."""
        with patch("cashu.lightning.stripe.settings") as mock_settings:
            mock_settings.mint_redis_url = "redis://localhost:6379"
            yield mock_settings

    @pytest.fixture
    def stripe_wallet(self, mock_settings):
        """Create a StripeWallet instance with mocked settings."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        return wallet

    def test_init_requires_redis_url(self):
        """Test that initialization fails without MINT_REDIS_URL."""
        with patch("cashu.lightning.stripe.settings") as mock_settings:
            mock_settings.mint_redis_url = None

            from cashu.lightning.stripe import StripeWallet

            with pytest.raises(ValueError, match="MINT_REDIS_URL is required"):
                StripeWallet(unit=Unit.usd)

    def test_init_rejects_non_usd(self, mock_settings):
        """Test that non-USD units raise Unsupported exception."""
        from cashu.lightning.stripe import StripeWallet

        with pytest.raises(Unsupported, match="Unit sat is not supported"):
            StripeWallet(unit=Unit.sat)

    def test_init_rejects_eur(self, mock_settings):
        """Test that EUR unit raises Unsupported exception."""
        from cashu.lightning.stripe import StripeWallet

        with pytest.raises(Unsupported, match="Unit eur is not supported"):
            StripeWallet(unit=Unit.eur)

    @pytest.mark.asyncio
    async def test_create_invoice_returns_uuid(self, stripe_wallet):
        """Test that create_invoice returns a valid UUID as checking_id and payment_request."""
        result = await stripe_wallet.create_invoice(
            amount=Amount(Unit.usd, 100), memo="Test invoice"
        )

        assert result.ok is True
        assert result.error_message is None

        # Verify checking_id is a valid UUID
        parsed = uuid.UUID(result.checking_id)
        assert str(parsed) == result.checking_id

        # payment_request should equal checking_id
        assert result.payment_request == result.checking_id

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
        """Test that incoming payment stream (Redis pub/sub) is supported."""
        assert stripe_wallet.supports_incoming_payment_stream is True

    def test_mpp_not_supported(self, stripe_wallet):
        """Test that multi-path payments are not supported."""
        assert stripe_wallet.supports_mpp is False

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_requires_redis_package(self, stripe_wallet):
        """Test that paid_invoices_stream requires redis package."""
        with patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
            with pytest.raises(RuntimeError, match="redis package required"):
                async for _ in stripe_wallet.paid_invoices_stream():
                    pass

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_requires_redis_url(self):
        """Test that paid_invoices_stream requires MINT_REDIS_URL at stream time."""
        with patch("cashu.lightning.stripe.settings") as mock_settings:
            # Set redis_url for __init__ to pass
            mock_settings.mint_redis_url = "redis://localhost:6379"

            from cashu.lightning.stripe import StripeWallet

            wallet = StripeWallet(unit=Unit.usd)

            # Now remove redis_url so stream fails
            mock_settings.mint_redis_url = None

            with pytest.raises(RuntimeError, match="MINT_REDIS_URL"):
                async for _ in wallet.paid_invoices_stream():
                    pass

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_yields_checking_id(self, mock_settings):
        """Test that paid_invoices_stream yields checking_ids from Redis."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        test_checking_id = str(uuid.uuid4())

        # Create mock Redis client and pubsub
        mock_pubsub = AsyncMock()

        # Simulate a single message then stop
        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": test_checking_id.encode()}

        mock_pubsub.listen = mock_listen
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
        mock_redis.close = AsyncMock()

        with patch("redis.asyncio.from_url", new_callable=AsyncMock) as mock_from_url:
            mock_from_url.return_value = mock_redis

            received = []
            async for checking_id in wallet.paid_invoices_stream():
                received.append(checking_id)
                break  # Only consume one message

            assert len(received) == 1
            assert received[0] == test_checking_id

            # Verify Redis was configured correctly
            mock_from_url.assert_called_once_with("redis://localhost:6379")
            mock_pubsub.subscribe.assert_called_once_with("cashu:paid_invoices")
