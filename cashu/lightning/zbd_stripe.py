"""Composite ZBD + Stripe backend for Nutshell.

Wraps both ZBDWallet and StripeWallet so they can serve the same unit
(USD) simultaneously behind a single slot in the backends dict.

Backend selection at quote-creation time is handled by the ledger, which
calls ``get_backend(name)`` to obtain the appropriate sub-backend before
invoking ``create_invoice``.  The default sub-backend is ZBD.
"""

from typing import AsyncGenerator, List, Optional

from loguru import logger

from ..core.base import Amount, MeltQuote, Unit
from ..core.errors import NotAllowedError
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
from .stripe import StripeWallet
from .zbd import ZBDWallet

DEFAULT_BACKEND = "zbd"


class ZBDStripeWallet(LightningBackend):
    """Composite backend that multiplexes ZBDWallet and StripeWallet.

    Plugs into ``backends[Method.bolt11][Unit.usd]`` as a single backend.
    The ledger selects a sub-backend via ``get_backend(name)`` when a
    ``backend`` field is present on the mint-quote request.

    Attributes:
        supported_units: ``{Unit.usd}``
        supports_mpp: False (neither sub-backend supports MPP).
        supports_incoming_payment_stream: False (both use HTTP callbacks).
        supports_description: True (ZBD supports descriptions).
    """

    supported_units = {Unit.usd}
    supports_mpp = False
    supports_incoming_payment_stream = False
    supports_description = True  # ZBD (default) supports descriptions

    def __init__(self, unit: Unit, **kwargs):
        self.assert_unit_supported(unit)
        self.unit = unit

        self._zbd = ZBDWallet(unit=unit)
        self._stripe = StripeWallet(unit=unit)

        self._backends = {
            "zbd": self._zbd,
            "stripe": self._stripe,
        }

        logger.info(
            "ZBDStripeWallet initialized with sub-backends: "
            + ", ".join(self._backends.keys())
        )

    # ------------------------------------------------------------------
    # Sub-backend selection (called by the ledger)
    # ------------------------------------------------------------------

    def get_backend(self, name: Optional[str] = None) -> LightningBackend:
        """Return a sub-backend by name, or the default (ZBD)."""
        key = name or DEFAULT_BACKEND
        if key not in self._backends:
            raise NotAllowedError(
                f"Unknown backend '{key}'. "
                f"Available: {', '.join(self._backends.keys())}"
            )
        return self._backends[key]

    def backend_names(self) -> List[str]:
        """Return the names of all available sub-backends."""
        return list(self._backends.keys())

    # ------------------------------------------------------------------
    # LightningBackend interface — delegates to the default (ZBD)
    # ------------------------------------------------------------------

    async def status(self) -> StatusResponse:
        return await self._zbd.status()

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Create an invoice via the default (ZBD) backend."""
        return await self._zbd.create_invoice(
            amount, memo, description_hash, unhashed_description
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check invoice status across sub-backends.

        Tries ZBD first.  If ZBD returns UNKNOWN, falls back to Stripe.
        """
        status = await self._zbd.get_invoice_status(checking_id)
        if status.result != PaymentResult.UNKNOWN:
            return status
        return await self._stripe.get_invoice_status(checking_id)

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        raise Unsupported("Melt (pay_invoice) is disabled for ZBDStripeWallet")

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        raise Unsupported(
            "Melt (get_payment_status) is disabled for ZBDStripeWallet"
        )

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        raise Unsupported(
            "Melt (get_payment_quote) is disabled for ZBDStripeWallet"
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        raise NotImplementedError(
            "ZBDStripeWallet uses HTTP callbacks, not paid_invoices_stream"
        )
        yield ""  # pragma: no cover
