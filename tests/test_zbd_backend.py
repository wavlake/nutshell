"""Unit tests for ZBD Lightning backend.

Tests cover:
- Invoice creation with proper ZBD API mapping
- Invoice status checking with status mapping
- Melt operations raising Unsupported exceptions
- Supported units validation
- Webhook stream configuration
"""

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

        wallet = ZBDWallet(unit=Unit.sat)
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
        """Test that only sat unit is supported."""
        assert Unit.sat in zbd_wallet.supported_units
        assert Unit.usd not in zbd_wallet.supported_units
        assert Unit.eur not in zbd_wallet.supported_units
        assert Unit.msat not in zbd_wallet.supported_units

    def test_unit_not_supported_raises(self, mock_settings):
        """Test that unsupported units raise Unsupported exception."""
        from cashu.lightning.zbd import ZBDWallet

        with pytest.raises(Unsupported, match="Unit usd is not supported"):
            ZBDWallet(unit=Unit.usd)

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


class TestInvoiceStatusMapping:
    """Test the ZBD status to Nutshell PaymentResult mapping."""

    def test_status_mapping_values(self):
        """Test all expected status mappings exist."""
        from cashu.lightning.zbd import INVOICE_STATUS_MAP

        assert INVOICE_STATUS_MAP["pending"] == PaymentResult.PENDING
        assert INVOICE_STATUS_MAP["completed"] == PaymentResult.SETTLED
        assert INVOICE_STATUS_MAP["expired"] == PaymentResult.FAILED
        assert INVOICE_STATUS_MAP["error"] == PaymentResult.FAILED
