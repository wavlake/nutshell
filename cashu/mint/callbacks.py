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


async def _handle_payment_callback(
    request: Request, body: PaymentCallbackRequest, secret_attr: str, backend_name: str
) -> JSONResponse:
    """Shared handler for payment callback endpoints."""
    secret = getattr(settings, secret_attr, None)
    if not secret:
        logger.error(
            f"{backend_name} callback received but"
            f" {secret_attr.upper()} is not configured"
        )
        return JSONResponse(
            status_code=503,
            content={"detail": f"{backend_name} callbacks not configured"},
        )

    if not _verify_bearer_token(request, secret):
        logger.warning(f"{backend_name} callback rejected: invalid bearer token")
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid authorization"},
        )

    logger.info(f"{backend_name} callback received for checking_id={body.checking_id}")
    try:
        await ledger.invoice_callback_dispatcher(body.checking_id)
    except Exception:
        logger.exception(
            f"Failed to dispatch {backend_name} callback"
            f" for checking_id={body.checking_id}"
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal error"},
        )
    return JSONResponse(status_code=200, content={"status": "ok"})


@callback_router.post(
    "/v1/callbacks/stripe-payment",
    name="Stripe payment callback",
    summary="Receive payment confirmation from the Go API for Stripe payments.",
)
async def stripe_payment_callback(
    request: Request, body: PaymentCallbackRequest
) -> JSONResponse:
    return await _handle_payment_callback(
        request, body, "mint_stripe_callback_secret", "Stripe"
    )


@callback_router.post(
    "/v1/callbacks/zbd-payment",
    name="ZBD payment callback",
    summary="Receive payment confirmation from the Go API for ZBD payments.",
)
async def zbd_payment_callback(
    request: Request, body: PaymentCallbackRequest
) -> JSONResponse:
    return await _handle_payment_callback(
        request, body, "mint_zbd_callback_secret", "ZBD"
    )
