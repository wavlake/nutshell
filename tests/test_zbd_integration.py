"""Integration tests for ZBD Lightning backend against real ZBD sandbox API.

These tests validate the ZBDWallet class works correctly with the real ZBD API.
They are skipped if MINT_ZBD_API_KEY is not set.

Run with:
    cd services/nutshell
    poetry run pytest tests/test_zbd_integration.py -v

Or with task:
    task credits:test:zbd:integration
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# Skip all tests if MINT_ZBD_API_KEY is not set
pytestmark = pytest.mark.skipif(
    not os.getenv("MINT_ZBD_API_KEY"),
    reason="MINT_ZBD_API_KEY not set - skipping ZBD integration tests",
)


@pytest.fixture
def zbd_settings():
    """Create mock settings with real API key for integration tests."""
    mock_settings = MagicMock()
    mock_settings.mint_zbd_api_key = os.getenv("MINT_ZBD_API_KEY")
    mock_settings.mint_zbd_endpoint = os.getenv(
        "MINT_ZBD_ENDPOINT", "https://api.zebedee.io"
    )
    mock_settings.mint_zbd_callback_url = ""
    mock_settings.mint_redis_url = None
    return mock_settings


@pytest.fixture
def zbd_wallet_sat(zbd_settings):
    """Create a ZBDWallet instance for sat unit with real API key."""
    with patch("cashu.lightning.zbd.settings", zbd_settings):
        from cashu.core.base import Unit
        from cashu.lightning.zbd import ZBDWallet

        ZBDWallet._rate_cache = None  # Clear cache between tests
        return ZBDWallet(unit=Unit.sat)


@pytest.fixture
def zbd_wallet_usd(zbd_settings):
    """Create a ZBDWallet instance for USD unit with real API key."""
    with patch("cashu.lightning.zbd.settings", zbd_settings):
        from cashu.core.base import Unit
        from cashu.lightning.zbd import ZBDWallet

        ZBDWallet._rate_cache = None  # Clear cache between tests
        return ZBDWallet(unit=Unit.usd)


class TestZBDIntegrationSat:
    """Integration tests for ZBDWallet with sat denomination."""

    @pytest.mark.asyncio
    async def test_status_returns_response(self, zbd_wallet_sat):
        """Test that status() returns a valid response from real ZBD API.

        Note: The /v1/wallet endpoint may not be available for all ZBD API key types
        (e.g., sandbox keys). The test verifies the method handles this gracefully
        by returning a StatusResponse with zero balance and an error message,
        rather than raising an exception.
        """
        from cashu.core.base import Unit

        status = await zbd_wallet_sat.status()

        # Status should always return a StatusResponse (not raise)
        assert status is not None
        assert status.balance is not None
        assert status.balance.unit == Unit.sat

        # Balance is either valid (>=0) or zero with error message
        if status.error_message is None:
            # Endpoint available: balance should be non-negative
            assert status.balance.amount >= 0
        else:
            # Endpoint not available (e.g., sandbox API key): balance should be 0
            # This is expected for sandbox/test API keys where /v1/wallet returns 404
            assert status.balance.amount == 0
            assert "status check failed" in status.error_message.lower()

    @pytest.mark.asyncio
    async def test_create_invoice_sat(self, zbd_wallet_sat):
        """Test creating a real Lightning invoice with sat denomination."""
        from cashu.core.base import Amount, Unit

        result = await zbd_wallet_sat.create_invoice(
            amount=Amount(Unit.sat, 1000),  # 1000 sats
            memo="ZBD integration test - sat",
        )

        assert result.ok is True, f"Invoice creation failed: {result.error_message}"
        assert result.checking_id is not None
        assert len(result.checking_id) > 0
        assert result.payment_request is not None
        assert result.payment_request.startswith("lnbc")

    @pytest.mark.asyncio
    async def test_get_invoice_status(self, zbd_wallet_sat):
        """Test checking invoice status on a real invoice."""
        from cashu.core.base import Amount, Unit
        from cashu.lightning.base import PaymentResult

        # First create an invoice
        invoice = await zbd_wallet_sat.create_invoice(
            amount=Amount(Unit.sat, 100),
            memo="Status check test",
        )
        assert invoice.ok is True

        # Check its status (should be pending since we didn't pay it)
        status = await zbd_wallet_sat.get_invoice_status(invoice.checking_id)

        assert status.result in [PaymentResult.PENDING, PaymentResult.UNKNOWN]
        # Should not be settled since we didn't pay
        assert status.result != PaymentResult.SETTLED


class TestZBDIntegrationUSD:
    """Integration tests for ZBDWallet with USD denomination."""

    @pytest.mark.asyncio
    async def test_get_exchange_rate(self, zbd_wallet_usd):
        """Test fetching real exchange rate from ZBD API."""
        rate = await zbd_wallet_usd.get_exchange_rate()

        # Rate should be a positive number in reasonable range
        # (BTC price between $10k and $1M seems safe for tests)
        assert rate > 10000
        assert rate < 1000000

    @pytest.mark.asyncio
    async def test_exchange_rate_caching(self, zbd_wallet_usd):
        """Test that exchange rate is properly cached."""
        from cashu.lightning.zbd import ZBDWallet

        # First fetch should hit the API
        rate1 = await zbd_wallet_usd.get_exchange_rate()
        assert ZBDWallet._rate_cache is not None

        # Second fetch should use cache (same rate)
        rate2 = await zbd_wallet_usd.get_exchange_rate()
        assert rate1 == rate2

    @pytest.mark.asyncio
    async def test_create_invoice_usd(self, zbd_wallet_usd):
        """Test creating a real Lightning invoice with USD denomination."""
        from cashu.core.base import Amount, Unit

        # Create invoice for $0.10 (10 cents)
        result = await zbd_wallet_usd.create_invoice(
            amount=Amount(Unit.usd, 10),  # 10 cents = $0.10
            memo="ZBD integration test - USD",
        )

        assert result.ok is True, f"USD invoice creation failed: {result.error_message}"
        assert result.checking_id is not None
        assert len(result.checking_id) > 0
        assert result.payment_request is not None
        assert result.payment_request.startswith("lnbc")

    @pytest.mark.asyncio
    async def test_create_invoice_usd_small_amount(self, zbd_wallet_usd):
        """Test creating invoice for minimum amount ($0.01)."""
        from cashu.core.base import Amount, Unit

        # Create invoice for $0.01 (1 cent) - minimum streaming credit
        result = await zbd_wallet_usd.create_invoice(
            amount=Amount(Unit.usd, 1),  # 1 cent = $0.01
            memo="ZBD integration test - 1 cent",
        )

        assert result.ok is True, f"1 cent invoice failed: {result.error_message}"
        assert result.checking_id is not None
        assert result.payment_request.startswith("lnbc")

    @pytest.mark.asyncio
    async def test_create_invoice_usd_larger_amount(self, zbd_wallet_usd):
        """Test creating invoice for larger amount ($1.00)."""
        from cashu.core.base import Amount, Unit

        # Create invoice for $1.00 (100 cents)
        result = await zbd_wallet_usd.create_invoice(
            amount=Amount(Unit.usd, 100),  # 100 cents = $1.00
            memo="ZBD integration test - $1.00",
        )

        assert result.ok is True, f"$1.00 invoice failed: {result.error_message}"
        assert result.checking_id is not None
        assert result.payment_request.startswith("lnbc")


class TestUSDConversionAccuracy:
    """Tests for USD to msats conversion accuracy."""

    @pytest.mark.asyncio
    async def test_conversion_divisible_by_1000(self, zbd_wallet_usd):
        """Test that converted amounts are divisible by 1000 (ZBD requirement)."""
        rate = await zbd_wallet_usd.get_exchange_rate()

        test_amounts = [1, 5, 10, 50, 100, 500, 1000]
        for cents in test_amounts:
            msats = zbd_wallet_usd.cents_to_msats(cents, rate)
            assert msats % 1000 == 0, (
                f"Amount for {cents} cents ({msats} msats) not divisible by 1000"
            )

    @pytest.mark.asyncio
    async def test_conversion_positive(self, zbd_wallet_usd):
        """Test that converted amounts are always positive."""
        rate = await zbd_wallet_usd.get_exchange_rate()

        test_amounts = [1, 10, 100, 1000]
        for cents in test_amounts:
            msats = zbd_wallet_usd.cents_to_msats(cents, rate)
            assert msats > 0, f"Amount for {cents} cents should be positive"

    @pytest.mark.asyncio
    async def test_conversion_scales_linearly(self, zbd_wallet_usd):
        """Test that conversion scales roughly linearly with amount."""
        rate = await zbd_wallet_usd.get_exchange_rate()

        msats_1 = zbd_wallet_usd.cents_to_msats(1, rate)
        msats_10 = zbd_wallet_usd.cents_to_msats(10, rate)
        msats_100 = zbd_wallet_usd.cents_to_msats(100, rate)

        # 10x the cents should give ~10x the msats (allowing for rounding)
        assert 9 <= msats_10 / msats_1 <= 11
        assert 9 <= msats_100 / msats_10 <= 11


class TestMeltDisabled:
    """Tests verifying melt operations are properly disabled."""

    @pytest.mark.asyncio
    async def test_pay_invoice_raises_unsupported(self, zbd_wallet_sat):
        """Test that pay_invoice raises Unsupported exception."""
        from unittest.mock import MagicMock

        from cashu.lightning.base import Unsupported

        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet_sat.pay_invoice(MagicMock(), 1000)

    @pytest.mark.asyncio
    async def test_get_payment_status_raises_unsupported(self, zbd_wallet_sat):
        """Test that get_payment_status raises Unsupported exception."""
        from cashu.lightning.base import Unsupported

        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet_sat.get_payment_status("test_id")

    @pytest.mark.asyncio
    async def test_get_payment_quote_raises_unsupported(self, zbd_wallet_sat):
        """Test that get_payment_quote raises Unsupported exception."""
        from unittest.mock import MagicMock

        from cashu.lightning.base import Unsupported

        with pytest.raises(Unsupported, match="Melt.*disabled"):
            await zbd_wallet_sat.get_payment_quote(MagicMock())
