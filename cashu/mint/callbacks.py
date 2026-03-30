"""HTTP callback endpoints for external payment notifications.

These endpoints receive payment confirmations from the Go API for backends
that don't use Lightning's native payment stream (Stripe, ZBD). The Go API
authenticates using a bearer token over HTTPS.
"""

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from ..core.settings import settings
from .startup import ledger

callback_router = APIRouter()


class PaymentCallbackRequest(BaseModel):
    checking_id: str


def _verify_bearer_token(request: Request, expected_secret: str) -> bool:
    """Verify the Authorization header contains the expected bearer token."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]
    return hmac.compare_digest(token, expected_secret)


@callback_router.post(
    "/v1/callbacks/stripe-payment",
    name="Stripe payment callback",
    summary="Receive payment confirmation from the Go API for Stripe payments.",
)
async def stripe_payment_callback(
    request: Request, body: PaymentCallbackRequest
) -> JSONResponse:
    secret = getattr(settings, "mint_stripe_callback_secret", None)
    if not secret:
        logger.error("Stripe callback received but MINT_STRIPE_CALLBACK_SECRET is not configured")
        return JSONResponse(
            status_code=503,
            content={"detail": "Stripe callbacks not configured"},
        )

    if not _verify_bearer_token(request, secret):
        logger.warning(f"Stripe callback rejected: invalid bearer token for checking_id={body.checking_id}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid authorization"},
        )

    logger.info(f"Stripe callback received for checking_id={body.checking_id}")
    await ledger.invoice_callback_dispatcher(body.checking_id)
    return JSONResponse(status_code=200, content={"status": "ok"})


@callback_router.post(
    "/v1/callbacks/zbd-payment",
    name="ZBD payment callback",
    summary="Receive payment confirmation from the Go API for ZBD payments.",
)
async def zbd_payment_callback(
    request: Request, body: PaymentCallbackRequest
) -> JSONResponse:
    secret = getattr(settings, "mint_zbd_callback_secret", None)
    if not secret:
        logger.error("ZBD callback received but MINT_ZBD_CALLBACK_SECRET is not configured")
        return JSONResponse(
            status_code=503,
            content={"detail": "ZBD callbacks not configured"},
        )

    if not _verify_bearer_token(request, secret):
        logger.warning(f"ZBD callback rejected: invalid bearer token for checking_id={body.checking_id}")
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid authorization"},
        )

    logger.info(f"ZBD callback received for checking_id={body.checking_id}")
    await ledger.invoice_callback_dispatcher(body.checking_id)
    return JSONResponse(status_code=200, content={"status": "ok"})
