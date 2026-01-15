"""Unit tests for ZBD Lightning backend.

Tests cover:
- Invoice creation with proper ZBD API mapping (sat and USD)
- Invoice status checking with status mapping
- Melt operations raising Unsupported exceptions
- Supported units validation (sat and usd)
- USD exchange rate fetching and caching
- Webhook stream configuration
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cashu.core.base import Amount, Unit
from cashu.lightning.base import PaymentResult, Unsupported


class TestZBDWallet:
    """Test suite for ZBDWallet Lightning backend."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings for ZBD backend."""
        with patch("cashu.lightning.zbd.settings") as mock_settings:
            mock_settings.mint_zbd_api_key = "test_api_key"
            mock_settings.mint_zbd_endpoint = "https://api.zebedee.io"
            mock_settings.mint_zbd_callback_url = "https://example.com/webhook"
            mock_settings.mint_redis_url = "redis://localhost:6379"
            yield mock_settings

    @pytest.fixture
    def zbd_wallet(self, mock_settings):
        """Create a ZBDWallet instance with mocked settings."""
        from cashu.lightning.zbd import ZBDWallet

        # Clear the rate cache between tests
        ZBDWallet._rate_cache = None
        wallet = ZBDWallet(unit=Unit.sat)
        return wallet

    @pytest.fixture
    def zbd_wallet_usd(self, mock_settings):
        """Create a ZBDWallet instance for USD unit."""
        from cashu.lightning.zbd import ZBDWallet

        # Clear the rate cache between tests
        ZBDWallet._rate_cache = None
        wallet = ZBDWallet(unit=Unit.usd)
        return wallet

    def test_init_requires_api_key(self):
        """Test that initialization fails without API key."""
        with patch("cashu.lightning.zbd.settings") as mock_settings:
            mock_settings.mint_zbd_api_key = None
            mock_settings.mint_zbd_endpoint = "https://api.zebedee.io"
            mock_settings.mint_zbd_callback_url = ""

            from cashu.lightning.zbd import ZBDWallet

            with pytest.raises(ValueError, match="MINT_ZBD_API_KEY is required"):
                ZBDWallet(unit=Unit.sat)

    def test_supported_units(self, zbd_wallet):
        """Test that sat and usd units are supported."""
        assert Unit.sat in zbd_wallet.supported_units
        assert Unit.usd in zbd_wallet.supported_units
        assert Unit.eur not in zbd_wallet.supported_units
        assert Unit.msat not in zbd_wallet.supported_units

    def test_unit_not_supported_raises(self, mock_settings):
        """Test that unsupported units raise Unsupported exception."""
        from cashu.lightning.zbd import ZBDWallet

        with pytest.raises(Unsupported, match="Unit eur is not supported"):
            ZBDWallet(unit=Unit.eur)

    def test_supports_incoming_payment_stream(self, zbd_wallet):
        """Test that webhook stream is supported."""
        assert zbd_wallet.supports_incoming_payment_stream is True

    def test_supports_description(self, zbd_wallet):
        """Test that invoice descriptions are supported."""
        assert zbd_wallet.supports_description is True

    def test_mpp_not_supported(self, zbd_wallet):
        """Test that multi-path payments are not supported."""
        assert zbd_wallet.supports_mpp is False

    @pytest.mark.asyncio
    async def test_status_success(self, zbd_wallet):
        """Test successful status check returns balance."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"balance": 1000000}  # 1000 sats in msats
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.status()

            assert result.error_message is None
            assert result.balance.amount == 1000  # 1000 sats
            assert result.balance.unit == Unit.sat
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_status_error_returns_zero_balance(self, zbd_wallet):
        """Test that status errors return zero balance with error message."""
        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("Connection failed")

            result = await zbd_wallet.status()

            assert result.error_message is not None
            assert "ZBD status check failed" in result.error_message
            assert result.balance.amount == 0

    @pytest.mark.asyncio
    async def test_create_invoice_success(self, zbd_wallet):
        """Test successful invoice creation."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "id": "charge_123",
                "invoice": {"request": "lnbc1000n1..."},
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = mock_response

            result = await zbd_wallet.create_invoice(
                amount=Amount(Unit.sat, 1000), memo="Test invoice"
            )

            assert result.ok is True
            assert result.checking_id == "charge_123"
            assert result.payment_request == "lnbc1000n1..."
            assert result.error_message is None

            # Verify correct API call
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            payload = call_args.kwargs["json"]
            assert payload["amount"] == "1000000"  # 1000 sats in msats
            assert payload["description"] == "Test invoice"
            assert payload["expiresIn"] == 900
            assert payload["callbackUrl"] == "https://example.com/webhook"

    @pytest.mark.asyncio
    async def test_create_invoice_default_memo(self, zbd_wallet):
        """Test invoice creation uses default memo when none provided."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {
                "id": "charge_456",
                "invoice": {"request": "lnbc500n1..."},
            }
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = mock_response

            result = await zbd_wallet.create_invoice(
                amount=Amount(Unit.sat, 500), memo=None
            )

            assert result.ok is True
            call_args = mock_post.call_args
            payload = call_args.kwargs["json"]
            assert payload["description"] == "Wavlake streaming credits"

    @pytest.mark.asyncio
    async def test_create_invoice_http_error(self, zbd_wallet):
        """Test invoice creation handles HTTP errors gracefully."""
        import httpx

        mock_response = MagicMock()
        mock_response.text = "Bad Request"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request", request=MagicMock(), response=mock_response
        )

        with patch.object(
            zbd_wallet.client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = mock_response

            result = await zbd_wallet.create_invoice(
                amount=Amount(Unit.sat, 1000), memo="Test"
            )

            assert result.ok is False
            assert "ZBD create_invoice failed" in result.error_message

    @pytest.mark.asyncio
    async def test_create_invoice_generic_error(self, zbd_wallet):
        """Test invoice creation handles generic exceptions gracefully."""
        with patch.object(
            zbd_wallet.client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.side_effect = Exception("Network error")

            result = await zbd_wallet.create_invoice(
                amount=Amount(Unit.sat, 1000), memo="Test"
            )

            assert result.ok is False
            assert "ZBD create_invoice failed" in result.error_message

    @pytest.mark.asyncio
    async def test_get_invoice_status_pending(self, zbd_wallet):
        """Test invoice status check returns PENDING."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"status": "pending"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.PENDING

    @pytest.mark.asyncio
    async def test_get_invoice_status_completed(self, zbd_wallet):
        """Test invoice status check returns SETTLED for completed."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"status": "completed"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.SETTLED

    @pytest.mark.asyncio
    async def test_get_invoice_status_expired(self, zbd_wallet):
        """Test invoice status check returns FAILED for expired."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"status": "expired"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.FAILED

    @pytest.mark.asyncio
    async def test_get_invoice_status_error(self, zbd_wallet):
        """Test invoice status check returns FAILED for error status."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"status": "error"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.FAILED

    @pytest.mark.asyncio
    async def test_get_invoice_status_unknown(self, zbd_wallet):
        """Test invoice status check returns UNKNOWN for unknown status."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": {"status": "some_new_status"}}
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.UNKNOWN

    @pytest.mark.asyncio
    async def test_get_invoice_status_exception(self, zbd_wallet):
        """Test invoice status check handles exceptions gracefully."""
        with patch.object(
            zbd_wallet.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("Network error")

            result = await zbd_wallet.get_invoice_status("charge_123")

            assert result.result == PaymentResult.UNKNOWN
            assert "ZBD get_invoice_status failed" in result.error_message

    @pytest.mark.asyncio
    async def test_pay_invoice_raises_unsupported(self, zbd_wallet):
        """Test that pay_invoice raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet.pay_invoice(MagicMock(), 1000)

    @pytest.mark.asyncio
    async def test_get_payment_status_raises_unsupported(self, zbd_wallet):
        """Test that get_payment_status raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet.get_payment_status("payment_123")

    @pytest.mark.asyncio
    async def test_get_payment_quote_raises_unsupported(self, zbd_wallet):
        """Test that get_payment_quote raises Unsupported exception."""
        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet.get_payment_quote(MagicMock())

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_requires_redis(self, zbd_wallet):
        """Test that paid_invoices_stream requires redis package."""
        # Mock redis not being installed
        with patch.dict("sys.modules", {"redis": None, "redis.asyncio": None}):
            with pytest.raises(RuntimeError, match="redis package required"):
                async for _ in zbd_wallet.paid_invoices_stream():
                    pass

    @pytest.mark.asyncio
    async def test_paid_invoices_stream_requires_redis_url(self, mock_settings):
        """Test that paid_invoices_stream requires MINT_REDIS_URL."""
        from cashu.lightning.zbd import ZBDWallet

        mock_settings.mint_redis_url = None
        wallet = ZBDWallet(unit=Unit.sat)

        with pytest.raises(RuntimeError, match="MINT_REDIS_URL"):
            async for _ in wallet.paid_invoices_stream():
                pass


class TestUSDSupport:
    """Test suite for USD denomination support."""

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings for ZBD backend."""
        with patch("cashu.lightning.zbd.settings") as mock_settings:
            mock_settings.mint_zbd_api_key = "test_api_key"
            mock_settings.mint_zbd_endpoint = "https://api.zebedee.io"
            mock_settings.mint_zbd_callback_url = "https://example.com/webhook"
            mock_settings.mint_redis_url = "redis://localhost:6379"
            yield mock_settings

    @pytest.fixture
    def zbd_wallet_usd(self, mock_settings):
        """Create a ZBDWallet instance for USD unit."""
        from cashu.lightning.zbd import ZBDWallet

        # Clear the rate cache between tests
        ZBDWallet._rate_cache = None
        wallet = ZBDWallet(unit=Unit.usd)
        return wallet

    def test_cents_to_msats_conversion(self, zbd_wallet_usd):
        """Test USD cents to msats conversion formula.

        Note: Conversion rounds UP to the nearest whole sat (divisible by 1000 msats)
        to satisfy ZBD API requirements. Due to floating point precision,
        we check within a small tolerance (1 sat).
        """
        # At $100,000/BTC:
        # $1.00 (100 cents) = 0.00001 BTC = ~1000 sats = ~1,000,000 msats
        rate = 100000.0
        result = zbd_wallet_usd.cents_to_msats(100, rate)
        # Allow 1 sat tolerance for floating point
        assert 1000000 <= result <= 1002000

        # $0.01 (1 cent) = ~10 sats = ~10,000 msats
        result = zbd_wallet_usd.cents_to_msats(1, rate)
        assert 10000 <= result <= 11000

        # $10.00 (1000 cents) = ~10,000 sats = ~10,000,000 msats
        result = zbd_wallet_usd.cents_to_msats(1000, rate)
        assert 10000000 <= result <= 10010000

    def test_cents_to_msats_different_rate(self, zbd_wallet_usd):
        """Test conversion with different exchange rate."""
        # At $50,000/BTC:
        # $1.00 (100 cents) = 0.00002 BTC = ~2000 sats = ~2,000,000 msats
        rate = 50000.0
        result = zbd_wallet_usd.cents_to_msats(100, rate)
        # Allow 1 sat tolerance
        assert 2000000 <= result <= 2002000

    def test_cents_to_msats_rounds_up(self, zbd_wallet_usd):
        """Test that fractional sats are rounded UP to nearest whole sat."""
        # At $96,000/BTC (typical rate):
        # 1 cent = $0.01 / $96,000 * 100M = 10.416... sats
        # Should round UP to 11 sats = 11,000 msats
        rate = 96000.0
        result = zbd_wallet_usd.cents_to_msats(1, rate)
        assert result == 11000  # Rounded up from 10.416 sats

        # 10 cents = ~104.16 sats -> 105 sats
        result = zbd_wallet_usd.cents_to_msats(10, rate)
        assert result == 105000  # Rounded up from 104.16 sats

    def test_cents_to_msats_divisible_by_1000(self, zbd_wallet_usd):
        """Test that result is always divisible by 1000 (ZBD API requirement)."""
        # Test various rates and amounts
        test_cases = [
            (1, 96000.0),
            (10, 95000.0),
            (100, 97500.0),
            (1000, 98765.43),
        ]
        for cents, rate in test_cases:
            result = zbd_wallet_usd.cents_to_msats(cents, rate)
            assert result % 1000 == 0, f"Result {result} not divisible by 1000 for {cents} cents at rate {rate}"

    @pytest.mark.asyncio
    async def test_get_exchange_rate_success(self, zbd_wallet_usd):
        """Test successful exchange rate fetch."""
        from cashu.lightning.zbd import ZBDWallet

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"btcUsdPrice": "100000.00"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            rate = await zbd_wallet_usd.get_exchange_rate()

            assert rate == 100000.0
            mock_get.assert_called_once()
            # Verify cache was updated
            assert ZBDWallet._rate_cache is not None
            assert ZBDWallet._rate_cache.rate == 100000.0

    @pytest.mark.asyncio
    async def test_get_exchange_rate_uses_fresh_cache(self, zbd_wallet_usd):
        """Test that fresh cached rate is returned without API call."""
        from cashu.lightning.zbd import CachedRate, ZBDWallet

        # Set up fresh cache (just now)
        ZBDWallet._rate_cache = CachedRate(rate=99000.0, timestamp=time.time())

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            rate = await zbd_wallet_usd.get_exchange_rate()

            assert rate == 99000.0
            # API should NOT be called when cache is fresh
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_exchange_rate_refreshes_stale_cache(self, zbd_wallet_usd):
        """Test that stale cache triggers API refresh."""
        from cashu.lightning.zbd import CachedRate, ZBDWallet

        # Set up stale cache (6 minutes ago - past 5 min TTL)
        ZBDWallet._rate_cache = CachedRate(
            rate=99000.0, timestamp=time.time() - 360
        )

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"btcUsdPrice": "101000.00"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            rate = await zbd_wallet_usd.get_exchange_rate()

            assert rate == 101000.0  # New rate from API
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_exchange_rate_circuit_breaker_fallback(self, zbd_wallet_usd):
        """Test circuit breaker uses stale cache on API error."""
        from cashu.lightning.zbd import CachedRate, ZBDWallet

        # Set up stale but usable cache (10 minutes ago - past 5 min, within 15 min)
        ZBDWallet._rate_cache = CachedRate(
            rate=99000.0, timestamp=time.time() - 600
        )

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("API unavailable")

            rate = await zbd_wallet_usd.get_exchange_rate()

            # Should return cached rate as fallback
            assert rate == 99000.0
            mock_get.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_exchange_rate_fails_without_valid_cache(self, zbd_wallet_usd):
        """Test that rate fetch fails when no valid cache exists."""
        from cashu.lightning.zbd import ZBDWallet

        # No cache set
        ZBDWallet._rate_cache = None

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("API unavailable")

            with pytest.raises(RuntimeError, match="Failed to fetch exchange rate"):
                await zbd_wallet_usd.get_exchange_rate()

    @pytest.mark.asyncio
    async def test_get_exchange_rate_fails_with_expired_cache(self, zbd_wallet_usd):
        """Test that rate fetch fails when cache is expired (>15 min)."""
        from cashu.lightning.zbd import CachedRate, ZBDWallet

        # Set up expired cache (20 minutes ago - past 15 min circuit breaker)
        ZBDWallet._rate_cache = CachedRate(
            rate=99000.0, timestamp=time.time() - 1200
        )

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("API unavailable")

            with pytest.raises(RuntimeError, match="Failed to fetch exchange rate"):
                await zbd_wallet_usd.get_exchange_rate()

    @pytest.mark.asyncio
    async def test_get_exchange_rate_invalid_rate(self, zbd_wallet_usd):
        """Test that invalid rate (zero or negative) raises error."""
        from cashu.lightning.zbd import ZBDWallet

        ZBDWallet._rate_cache = None

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": {"btcUsdPrice": "0"}
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.return_value = mock_response

            with pytest.raises(RuntimeError, match="Failed to fetch exchange rate"):
                await zbd_wallet_usd.get_exchange_rate()

    @pytest.mark.asyncio
    async def test_create_invoice_usd_success(self, zbd_wallet_usd):
        """Test successful USD invoice creation with exchange rate."""
        mock_rate_response = MagicMock()
        mock_rate_response.json.return_value = {
            "data": {"btcUsdPrice": "100000.00"}
        }
        mock_rate_response.raise_for_status = MagicMock()

        mock_invoice_response = MagicMock()
        mock_invoice_response.json.return_value = {
            "data": {
                "id": "charge_usd_123",
                "invoice": {"request": "lnbc1000u1..."},
            }
        }
        mock_invoice_response.raise_for_status = MagicMock()

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get, patch.object(
            zbd_wallet_usd.client, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_get.return_value = mock_rate_response
            mock_post.return_value = mock_invoice_response

            # Create invoice for $1.00 (100 cents)
            result = await zbd_wallet_usd.create_invoice(
                amount=Amount(Unit.usd, 100), memo="USD test invoice"
            )

            assert result.ok is True
            assert result.checking_id == "charge_usd_123"
            assert result.payment_request == "lnbc1000u1..."

            # Verify correct msats amount (with tolerance for floating point)
            # $1.00 at $100,000/BTC = ~1000 sats = ~1,000,000 msats
            call_args = mock_post.call_args
            payload = call_args.kwargs["json"]
            amount_msats = int(payload["amount"])
            # Allow 2 sat tolerance for floating point rounding
            assert 1000000 <= amount_msats <= 1002000
            # Verify divisible by 1000 (ZBD requirement)
            assert amount_msats % 1000 == 0
            assert payload["description"] == "USD test invoice"

    @pytest.mark.asyncio
    async def test_create_invoice_usd_rate_error(self, zbd_wallet_usd):
        """Test USD invoice creation fails gracefully on rate error."""
        from cashu.lightning.zbd import ZBDWallet

        ZBDWallet._rate_cache = None  # Ensure no cache

        with patch.object(
            zbd_wallet_usd.client, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = Exception("Rate API unavailable")

            result = await zbd_wallet_usd.create_invoice(
                amount=Amount(Unit.usd, 100), memo="Test"
            )

            assert result.ok is False
            assert "Exchange rate error" in result.error_message


class TestCachedRate:
    """Test the CachedRate dataclass."""

    def test_is_fresh_within_ttl(self):
        """Test that rate within 5 min TTL is fresh."""
        from cashu.lightning.zbd import CachedRate

        # 2 minutes ago
        rate = CachedRate(rate=100000.0, timestamp=time.time() - 120)
        assert rate.is_fresh() is True

    def test_is_fresh_past_ttl(self):
        """Test that rate past 5 min TTL is not fresh."""
        from cashu.lightning.zbd import CachedRate

        # 6 minutes ago
        rate = CachedRate(rate=100000.0, timestamp=time.time() - 360)
        assert rate.is_fresh() is False

    def test_is_usable_within_circuit_breaker(self):
        """Test that rate within 15 min is usable."""
        from cashu.lightning.zbd import CachedRate

        # 10 minutes ago
        rate = CachedRate(rate=100000.0, timestamp=time.time() - 600)
        assert rate.is_usable() is True

    def test_is_usable_past_circuit_breaker(self):
        """Test that rate past 15 min is not usable."""
        from cashu.lightning.zbd import CachedRate

        # 20 minutes ago
        rate = CachedRate(rate=100000.0, timestamp=time.time() - 1200)
        assert rate.is_usable() is False


class TestInvoiceStatusMapping:
    """Test the ZBD status to Nutshell PaymentResult mapping."""

    def test_status_mapping_values(self):
        """Test all expected status mappings exist."""
        from cashu.lightning.zbd import INVOICE_STATUS_MAP

        assert INVOICE_STATUS_MAP["pending"] == PaymentResult.PENDING
        assert INVOICE_STATUS_MAP["completed"] == PaymentResult.SETTLED
        assert INVOICE_STATUS_MAP["expired"] == PaymentResult.FAILED
        assert INVOICE_STATUS_MAP["error"] == PaymentResult.FAILED
