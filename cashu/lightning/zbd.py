"""ZBD Lightning backend for Nutshell.

This module provides a Lightning backend implementation that interfaces directly
with the ZBD API for invoice creation and status checking. It is designed for
melt-disabled configurations (consumption-only tokens) and supports webhook-based
payment notifications via Redis pub/sub.

Supports both sat and USD denominations. USD amounts are converted to msats using
ZBD's exchange rate API with caching and circuit breaker fallback.

https://docs.zbdpay.com/docs
"""

import time
from dataclasses import dataclass
from typing import AsyncGenerator, Optional

import httpx

from ..core.base import Amount, MeltQuote, Unit
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
    Unsupported,
)

# ZBD status to Nutshell PaymentResult mapping
INVOICE_STATUS_MAP = {
    "pending": PaymentResult.PENDING,
    "completed": PaymentResult.SETTLED,
    "expired": PaymentResult.FAILED,
    "error": PaymentResult.FAILED,
}

# Exchange rate caching configuration
RATE_CACHE_TTL_SECONDS = 300  # 5 minutes
RATE_CIRCUIT_BREAKER_TTL_SECONDS = 900  # 15 minutes (fallback)


@dataclass
class CachedRate:
    """Cached exchange rate with timestamp."""

    rate: float  # BTC/USD rate (e.g., 100000.00)
    timestamp: float  # Unix timestamp when rate was fetched

    def is_fresh(self) -> bool:
        """Check if rate is within normal cache TTL (5 minutes)."""
        return time.time() - self.timestamp < RATE_CACHE_TTL_SECONDS

    def is_usable(self) -> bool:
        """Check if rate is within circuit breaker TTL (15 minutes)."""
        return time.time() - self.timestamp < RATE_CIRCUIT_BREAKER_TTL_SECONDS


class ZBDWallet(LightningBackend):
    """ZBD Lightning backend for Nutshell.

    Direct integration with ZBD API, supporting webhooks for instant
    payment notification. Designed for melt-disabled configurations
    where tokens are consumption-only (no redemption for Lightning).

    Supports both sat and USD denominations. USD amounts are converted
    to msats using the BTC/USD exchange rate from ZBD's API.

    API Reference: https://docs.zbdpay.com/docs

    Attributes:
        supported_units: Set of supported currency units (sat and usd).
        supports_mpp: Multi-path payment support (disabled).
        supports_incoming_payment_stream: Webhook support via Redis (enabled).
        supports_description: Invoice description support (enabled).
        unit: The currency unit for this backend instance.
    """

    supported_units = {Unit.sat, Unit.usd}
    supports_mpp = False
    supports_incoming_payment_stream = True
    supports_description = True

    # Class-level exchange rate cache (shared across instances)
    _rate_cache: Optional[CachedRate] = None

    def __init__(self, unit: Unit, **kwargs):
        """Initialize ZBD wallet backend.

        Args:
            unit: Currency unit (sat or usd).
            **kwargs: Additional arguments (unused).

        Raises:
            Unsupported: If unit is not sat or usd.
            ValueError: If MINT_ZBD_API_KEY is not configured.
        """
        self.assert_unit_supported(unit)
        self.unit = unit
        self.endpoint = settings.mint_zbd_endpoint or "https://api.zebedee.io"
        self.callback_url = getattr(settings, "mint_zbd_callback_url", None) or ""

        api_key = getattr(settings, "mint_zbd_api_key", None)
        if not api_key:
            raise ValueError("MINT_ZBD_API_KEY is required for ZBDWallet")

        self.client = httpx.AsyncClient(
            headers={"apikey": api_key},
            timeout=30.0,
        )

    async def status(self) -> StatusResponse:
        """Check wallet status and balance.

        Returns:
            StatusResponse with balance in satoshis and any error message.
        """
        try:
            r = await self.client.get(f"{self.endpoint}/v1/wallet")
            r.raise_for_status()
            data = r.json()

            # ZBD returns balance in msats
            balance_msats = int(data.get("data", {}).get("balance", 0))
            balance_sats = balance_msats // 1000

            return StatusResponse(
                error_message=None,
                balance=Amount(Unit.sat, balance_sats),
            )
        except Exception as e:
            return StatusResponse(
                error_message=f"ZBD status check failed: {e}",
                balance=Amount(Unit.sat, 0),
            )

    async def get_exchange_rate(self) -> float:
        """Fetch BTC/USD exchange rate from ZBD API with caching.

        Implements a caching strategy with circuit breaker:
        - Fresh cache (< 5 min): Use cached rate
        - Stale cache (5-15 min): Try to refresh, fall back to cached on error
        - Expired cache (> 15 min): Must fetch fresh rate

        Returns:
            BTC/USD exchange rate (e.g., 100000.00 for $100,000/BTC)

        Raises:
            RuntimeError: If rate cannot be fetched and no valid cached rate exists
        """
        # Check if we have a fresh cached rate
        if ZBDWallet._rate_cache is not None and ZBDWallet._rate_cache.is_fresh():
            return ZBDWallet._rate_cache.rate

        # Try to fetch fresh rate
        try:
            r = await self.client.get(f"{self.endpoint}/v1/btcusd")
            r.raise_for_status()
            data = r.json().get("data", {})

            rate = float(data.get("btcUsdPrice", 0))
            if rate <= 0:
                raise ValueError("Invalid exchange rate received from ZBD")

            # Update cache with fresh rate
            ZBDWallet._rate_cache = CachedRate(rate=rate, timestamp=time.time())
            return rate

        except Exception as e:
            # Circuit breaker: if we have a stale-but-usable cached rate, use it
            if ZBDWallet._rate_cache is not None and ZBDWallet._rate_cache.is_usable():
                return ZBDWallet._rate_cache.rate

            # No valid cached rate available
            raise RuntimeError(f"Failed to fetch exchange rate from ZBD: {e}")

    def cents_to_msats(self, cents: int, btc_usd_rate: float) -> int:
        """Convert USD cents to millisatoshis using the given exchange rate.

        Formula: msats = (cents / 100 / btc_usd_rate) * 100_000_000 * 1000

        Args:
            cents: Amount in USD cents (e.g., 100 for $1.00)
            btc_usd_rate: BTC/USD exchange rate (e.g., 100000.00)

        Returns:
            Amount in millisatoshis
        """
        dollars = cents / 100
        btc = dollars / btc_usd_rate
        sats = btc * 100_000_000
        msats = int(sats * 1000)
        return msats

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Create a Lightning invoice via ZBD.

        Args:
            amount: Amount for the invoice (sat or usd).
            memo: Optional description text.
            description_hash: Optional description hash (unused by ZBD).
            unhashed_description: Optional unhashed description (unused by ZBD).

        Returns:
            InvoiceResponse with checking_id and payment_request bolt11 string.

        Note:
            For USD amounts, the exchange rate is fetched and locked at quote
            creation time. The rate used for conversion is cached for up to
            5 minutes, with a 15-minute circuit breaker fallback.
        """
        self.assert_unit_supported(amount.unit)

        # Convert to millisatoshis for ZBD API
        try:
            if amount.unit == Unit.sat:
                amount_msats = str(amount.amount * 1000)
            elif amount.unit == Unit.usd:
                # Fetch exchange rate and convert USD cents to msats
                rate = await self.get_exchange_rate()
                amount_msats = str(self.cents_to_msats(amount.amount, rate))
            else:
                return InvoiceResponse(
                    ok=False,
                    error_message=f"Unsupported unit: {amount.unit}",
                )
        except RuntimeError as e:
            return InvoiceResponse(
                ok=False,
                error_message=f"Exchange rate error: {e}",
            )

        payload = {
            "amount": amount_msats,
            "description": memo or "Wavlake streaming credits",
            "expiresIn": 900,  # 15 minutes
        }

        # Add callback URL if configured (for webhook notifications)
        if self.callback_url:
            payload["callbackUrl"] = self.callback_url

        try:
            r = await self.client.post(f"{self.endpoint}/v1/charges", json=payload)
            r.raise_for_status()
            data = r.json().get("data", {})

            return InvoiceResponse(
                ok=True,
                checking_id=data.get("id"),
                payment_request=data.get("invoice", {}).get("request"),
            )
        except httpx.HTTPStatusError as e:
            return InvoiceResponse(
                ok=False,
                error_message=f"ZBD create_invoice failed: {e.response.text}",
            )
        except Exception as e:
            return InvoiceResponse(
                ok=False,
                error_message=f"ZBD create_invoice failed: {e}",
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check if an invoice has been paid.

        Args:
            checking_id: The ZBD charge ID from create_invoice.

        Returns:
            PaymentStatus with result (PENDING, SETTLED, FAILED, or UNKNOWN).
        """
        try:
            r = await self.client.get(f"{self.endpoint}/v1/charges/{checking_id}")
            r.raise_for_status()
            data = r.json().get("data", {})

            status = data.get("status", "pending")
            return PaymentStatus(
                result=INVOICE_STATUS_MAP.get(status, PaymentResult.UNKNOWN),
            )
        except Exception as e:
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message=f"ZBD get_invoice_status failed: {e}",
            )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        """Pay a Lightning invoice. DISABLED for this backend.

        This backend is designed for melt-disabled configurations where
        tokens are consumption-only and cannot be redeemed for Lightning.

        Args:
            quote: Melt quote containing the bolt11 invoice.
            fee_limit_msat: Maximum fee in millisatoshis.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (pay_invoice) is disabled for ZBDWallet")

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check outgoing payment status. DISABLED for this backend.

        This backend is designed for melt-disabled configurations.

        Args:
            checking_id: The payment checking ID.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (get_payment_status) is disabled for ZBDWallet")

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        """Get quote for outgoing payment. DISABLED for this backend.

        This backend is designed for melt-disabled configurations.

        Args:
            melt_quote: Melt quote request.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (get_payment_quote) is disabled for ZBDWallet")

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Stream paid invoice IDs from Redis pub/sub.

        Webhooks from ZBD are received by the monorepo API and published
        to Redis. This method subscribes to that channel and yields
        invoice IDs (checking_ids) as they are paid.

        Yields:
            checking_id (str): The ZBD charge ID of the paid invoice.

        Raises:
            RuntimeError: If redis package is not installed or MINT_REDIS_URL
                is not configured.
        """
        # Import here to avoid hard dependency when not using webhooks
        try:
            import redis.asyncio as aioredis
        except ImportError:
            raise RuntimeError(
                "redis package required for paid_invoices_stream. "
                "Install with: pip install redis"
            )

        redis_url = getattr(settings, "mint_redis_url", None)
        if not redis_url:
            raise RuntimeError(
                "MINT_REDIS_URL environment variable required for paid_invoices_stream"
            )

        redis_client = await aioredis.from_url(redis_url)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe("cashu:paid_invoices")

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    checking_id = message["data"].decode()
                    yield checking_id
        finally:
            await pubsub.unsubscribe("cashu:paid_invoices")
            await redis_client.close()
