"""Stripe payment backend for Nutshell.

This module provides a Lightning backend implementation that accepts Stripe
payments via Redis pub/sub. It is designed for USD-only, melt-disabled
configurations where tokens are consumption-only.

No external Stripe API calls are made by this backend. The Go API handles
Stripe checkout sessions and webhooks, publishing payment confirmations
to Redis. This backend simply listens for those confirmations and marks
the corresponding mint quotes as paid.

Follows the same pattern as the ZBD backend for webhook-based payment
notification via Redis pub/sub on the `cashu:paid_invoices` channel.

Security: Redis messages must be HMAC-SHA256 signed by the Go API using
a shared secret (MINT_REDIS_HMAC_SECRET). Messages are in the format
`checking_id:hmac_hex`. Unsigned or invalid messages are rejected.
"""

import hashlib
import hmac
import uuid
from typing import AsyncGenerator, Optional

from loguru import logger

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


class StripeWallet(LightningBackend):
    """Stripe payment backend for Nutshell.

    Accepts Stripe payments via Redis pub/sub. The Go API publishes
    HMAC-signed checking_ids to the `cashu:paid_invoices` Redis channel
    when Stripe webhooks confirm payment. This backend subscribes to that
    channel, verifies the HMAC signature, and yields checking_ids for
    Nutshell's invoice_callback_dispatcher.

    USD-only. Melt operations are disabled.

    Attributes:
        supported_units: Set of supported currency units (usd only).
        supports_mpp: Multi-path payment support (disabled).
        supports_incoming_payment_stream: Redis pub/sub support (enabled).
        supports_description: Invoice description support (disabled).
        unit: The currency unit for this backend instance (always usd).
    """

    supported_units = {Unit.usd}
    supports_mpp = False
    supports_incoming_payment_stream = True
    supports_description = False

    def __init__(self, unit: Unit, **kwargs):
        """Initialize Stripe wallet backend.

        Args:
            unit: Currency unit (must be usd).
            **kwargs: Additional arguments (unused).

        Raises:
            Unsupported: If unit is not usd.
            ValueError: If MINT_REDIS_URL or MINT_REDIS_HMAC_SECRET is not configured.
        """
        self.assert_unit_supported(unit)
        self.unit = unit

        redis_url = getattr(settings, "mint_redis_url", None)
        if not redis_url:
            raise ValueError(
                "MINT_REDIS_URL is required for StripeWallet"
            )

        hmac_secret = getattr(settings, "mint_redis_hmac_secret", None)
        if not hmac_secret:
            raise ValueError(
                "MINT_REDIS_HMAC_SECRET is required for StripeWallet"
            )
        self._hmac_secret = hmac_secret.encode()

    async def status(self) -> StatusResponse:
        """Check wallet status.

        Returns:
            StatusResponse with zero balance and no error.
            Stripe balance is managed externally; this backend
            does not track balance.
        """
        return StatusResponse(
            error_message=None,
            balance=Amount(Unit.usd, 0),
        )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Create a mint quote identifier.

        No external API call is made. Generates a UUID as the checking_id.
        The payment_request is a separate client-facing identifier prefixed
        with "stripe:" that cannot be used to forge Redis payment messages.

        The Go API creates the actual Stripe checkout session separately
        and maps the quote to the Stripe session.

        Args:
            amount: Amount for the quote (must be usd).
            memo: Optional description (unused).
            description_hash: Optional description hash (unused).
            unhashed_description: Optional unhashed description (unused).

        Returns:
            InvoiceResponse with a UUID checking_id and a separate
            stripe-prefixed payment_request.
        """
        checking_id = str(uuid.uuid4())
        return InvoiceResponse(
            ok=True,
            checking_id=checking_id,
            payment_request=f"stripe:{checking_id}",
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check invoice payment status.

        Always returns PENDING. Actual state transitions are handled by
        paid_invoices_stream() and the ledger's invoice_callback_dispatcher.

        Args:
            checking_id: The quote checking ID.

        Returns:
            PaymentStatus with PENDING result.
        """
        return PaymentStatus(result=PaymentResult.PENDING)

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        """Pay an invoice. DISABLED for this backend.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (pay_invoice) is disabled for StripeWallet")

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check outgoing payment status. DISABLED for this backend.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (get_payment_status) is disabled for StripeWallet")

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        """Get quote for outgoing payment. DISABLED for this backend.

        Raises:
            Unsupported: Always raised - melt is disabled.
        """
        raise Unsupported("Melt (get_payment_quote) is disabled for StripeWallet")

    def _verify_hmac(self, message: str) -> Optional[str]:
        """Verify HMAC-SHA256 signature on a Redis payment message.

        Expected format: `checking_id:hmac_hex` where hmac_hex is
        HMAC-SHA256(checking_id, secret).hexdigest().

        Args:
            message: Raw message from Redis channel.

        Returns:
            The checking_id if signature is valid, None otherwise.
        """
        if ":" not in message:
            logger.warning("Rejected unsigned Redis message (no separator)")
            return None

        # Split on last colon — checking_id is a UUID (no colons)
        checking_id, received_hmac = message.rsplit(":", 1)

        expected_hmac = hmac.new(
            self._hmac_secret,
            checking_id.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_hmac, received_hmac):
            logger.warning(
                f"Rejected Redis message with invalid HMAC for id: {checking_id}"
            )
            return None

        return checking_id

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Stream paid invoice IDs from Redis pub/sub.

        The Go API publishes HMAC-signed checking_ids to the
        `cashu:paid_invoices` Redis channel when Stripe webhooks confirm
        payment. This method subscribes to that channel, verifies the
        HMAC signature, and yields checking_ids as they arrive.

        Message format: `checking_id:hmac_hex`

        Yields:
            checking_id (str): The UUID of the paid mint quote.

        Raises:
            RuntimeError: If redis package is not installed or MINT_REDIS_URL
                is not configured.
        """
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
                    raw = message["data"].decode()
                    checking_id = self._verify_hmac(raw)
                    if checking_id is not None:
                        yield checking_id
        finally:
            await pubsub.unsubscribe("cashu:paid_invoices")
            await redis_client.close()
