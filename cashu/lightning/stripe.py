"""Stripe payment backend for Nutshell.

This module provides a Lightning backend implementation that accepts Stripe
payments via HTTP callbacks. It is designed for USD-only, melt-disabled
configurations where tokens are consumption-only.

No external Stripe API calls are made by this backend. The Go API handles
Stripe checkout sessions and webhooks, then calls the mint's HTTP callback
endpoint to notify of payment. This backend simply provides invoice creation
and status checking.

Security: The Go API authenticates callback requests using a bearer token
(MINT_STRIPE_CALLBACK_SECRET) over HTTPS.
"""

import uuid
from typing import AsyncGenerator, Optional

from ..core.base import Amount, MeltQuote, Unit
from ..core.models import PostMeltQuoteRequest
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

    Accepts Stripe payments via HTTP callbacks from the Go API.
    The Go API calls the mint's /v1/callbacks/stripe-payment endpoint
    with a bearer token when Stripe webhooks confirm payment.

    USD-only. Melt operations are disabled.

    Attributes:
        supported_units: Set of supported currency units (usd only).
        supports_mpp: Multi-path payment support (disabled).
        supports_incoming_payment_stream: Disabled (uses HTTP callbacks instead).
        supports_description: Invoice description support (disabled).
        unit: The currency unit for this backend instance (always usd).
    """

    supported_units = {Unit.usd}
    supports_mpp = False
    supports_incoming_payment_stream = False
    supports_description = False

    def __init__(self, unit: Unit, **kwargs):
        """Initialize Stripe wallet backend.

        Args:
            unit: Currency unit (must be usd).
            **kwargs: Additional arguments (unused).

        Raises:
            Unsupported: If unit is not usd.
        """
        self.assert_unit_supported(unit)
        self.unit = unit

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
        with "stripe:" that cannot be used to forge payment messages.

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
        the HTTP callback endpoint and the ledger's invoice_callback_dispatcher.

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

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """Not used. Payment notifications arrive via HTTP callbacks."""
        raise NotImplementedError(
            "StripeWallet uses HTTP callbacks, not paid_invoices_stream"
        )
        yield ""  # pragma: no cover
