"""Unit tests for Stripe payment backend.

Tests cover:
- Initialization with required settings (mint_redis_url, mint_redis_hmac_secret, unit validation)
- Invoice creation returning UUID checking_id and stripe-prefixed payment_request
- Status returning zero balance
- Melt operations raising Unsupported exceptions
- Invoice status always returning PENDING
- Redis pub/sub stream for paid invoices with HMAC verification
"""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cashu.core.base import Amount, Unit
from cashu.lightning.base import PaymentResult, Unsupported

HMAC_SECRET = "test-hmac-secret-key"


def _sign(checking_id: str, secret: str = HMAC_SECRET) -> str:
    """Create HMAC-signed message in the expected format."""
    sig = hmac.new(
        secret.encode(), checking_id.encode(), hashlib.sha256
    ).hexdigest()
    return f"{checking_id}:{sig}"


class TestStripeWallet:
    """Test suite for StripeWallet payment backend."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings for Stripe backend."""
        with patch("cashu.lightning.stripe.settings") as mock_settings:
            mock_settings.mint_redis_url = "redis://localhost:6379"
            mock_settings.mint_redis_hmac_secret = HMAC_SECRET
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
            mock_settings.mint_redis_hmac_secret = HMAC_SECRET

            from cashu.lightning.stripe import StripeWallet

            with pytest.raises(ValueError, match="MINT_REDIS_URL is required"):
                StripeWallet(unit=Unit.usd)

    def test_init_requires_hmac_secret(self):
        """Test that initialization fails without MINT_REDIS_HMAC_SECRET."""
        with patch("cashu.lightning.stripe.settings") as mock_settings:
            mock_settings.mint_redis_url = "redis://localhost:6379"
            mock_settings.mint_redis_hmac_secret = None

            from cashu.lightning.stripe import StripeWallet

            with pytest.raises(ValueError, match="MINT_REDIS_HMAC_SECRET is required"):
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
            mock_settings.mint_redis_url = "redis://localhost:6379"
            mock_settings.mint_redis_hmac_secret = HMAC_SECRET

            from cashu.lightning.stripe import StripeWallet

            wallet = StripeWallet(unit=Unit.usd)

            # Now remove redis_url so stream fails
            mock_settings.mint_redis_url = None

            with pytest.raises(RuntimeError, match="MINT_REDIS_URL"):
                async for _ in wallet.paid_invoices_stream():
                    pass

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_yields_valid_hmac(self, mock_settings):
        """Test that paid_invoices_stream yields checking_ids with valid HMAC."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        test_checking_id = str(uuid.uuid4())
        signed_message = _sign(test_checking_id)

        mock_pubsub = AsyncMock()

        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": signed_message.encode()}

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
                break

            assert len(received) == 1
            assert received[0] == test_checking_id

            mock_from_url.assert_called_once_with("redis://localhost:6379")
            mock_pubsub.subscribe.assert_called_once_with("cashu:paid_invoices")

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_rejects_unsigned_message(self, mock_settings):
        """Test that unsigned messages (no colon separator) are rejected."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        unsigned_id = str(uuid.uuid4())

        mock_pubsub = AsyncMock()

        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            # Unsigned message — no colon separator
            yield {"type": "message", "data": unsigned_id.encode()}

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
                break

            # Should not yield anything for unsigned message
            assert len(received) == 0

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_rejects_invalid_hmac(self, mock_settings):
        """Test that messages with wrong HMAC are rejected."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        test_checking_id = str(uuid.uuid4())
        # Sign with wrong secret
        bad_message = _sign(test_checking_id, secret="wrong-secret")

        mock_pubsub = AsyncMock()

        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": bad_message.encode()}

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
                break

            # Should not yield anything for invalid HMAC
            assert len(received) == 0

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_rejects_tampered_id(self, mock_settings):
        """Test that a valid HMAC for a different checking_id is rejected."""
        from cashu.lightning.stripe import StripeWallet

        wallet = StripeWallet(unit=Unit.usd)
        original_id = str(uuid.uuid4())
        tampered_id = str(uuid.uuid4())
        # Sign with original_id but replace with tampered_id
        sig = _sign(original_id).split(":")[1]
        tampered_message = f"{tampered_id}:{sig}"

        mock_pubsub = AsyncMock()

        async def mock_listen():
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": tampered_message.encode()}

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
                break

            assert len(received) == 0

    def test_verify_hmac_valid(self, stripe_wallet):
        """Test _verify_hmac accepts valid signatures."""
        checking_id = str(uuid.uuid4())
        message = _sign(checking_id)

        result = stripe_wallet._verify_hmac(message)
        assert result == checking_id

    def test_verify_hmac_invalid(self, stripe_wallet):
        """Test _verify_hmac rejects invalid signatures."""
        checking_id = str(uuid.uuid4())
        message = f"{checking_id}:invalid_hmac"

        result = stripe_wallet._verify_hmac(message)
        assert result is None

    def test_verify_hmac_no_separator(self, stripe_wallet):
        """Test _verify_hmac rejects messages without separator."""
        result = stripe_wallet._verify_hmac("no-separator-here")
        assert result is None

    def test_verify_hmac_empty_string(self, stripe_wallet):
        """Test _verify_hmac rejects empty strings."""
        result = stripe_wallet._verify_hmac("")
        assert result is None
