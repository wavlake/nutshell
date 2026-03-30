"""Unit tests for HTTP callback endpoints.

Tests cover:
- Valid bearer token marks quote paid (200)
- Invalid/missing bearer token returns 401
- Missing callback secret config returns 503
- Both stripe and zbd callback paths
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_ledger():
    """Mock the ledger's invoice_callback_dispatcher."""
    with patch("cashu.mint.callbacks.ledger") as mock:
        mock.invoice_callback_dispatcher = AsyncMock()
        yield mock


@pytest.fixture
def mock_settings():
    """Mock settings with callback secrets configured."""
    with patch("cashu.mint.callbacks.settings") as mock:
        mock.mint_stripe_callback_secret = "test-stripe-secret"
        mock.mint_zbd_callback_secret = "test-zbd-secret"
        yield mock


@pytest.fixture
def client(mock_ledger, mock_settings):
    """Create a test client with the callback router."""
    from fastapi import FastAPI

    from cashu.mint.callbacks import callback_router

    app = FastAPI()
    app.include_router(callback_router)
    return TestClient(app)


class TestStripeCallback:
    """Tests for POST /v1/callbacks/stripe-payment."""

    def test_valid_bearer_token(self, client, mock_ledger):
        """Valid bearer token calls dispatcher and returns 200."""
        resp = client.post(
            "/v1/callbacks/stripe-payment",
            json={"checking_id": "abc-123"},
            headers={"Authorization": "Bearer test-stripe-secret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        mock_ledger.invoice_callback_dispatcher.assert_called_once_with("abc-123")

    def test_invalid_bearer_token(self, client, mock_ledger):
        """Invalid bearer token returns 401."""
        resp = client.post(
            "/v1/callbacks/stripe-payment",
            json={"checking_id": "abc-123"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_missing_authorization_header(self, client, mock_ledger):
        """Missing Authorization header returns 401."""
        resp = client.post(
            "/v1/callbacks/stripe-payment",
            json={"checking_id": "abc-123"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_missing_callback_secret_config(self, mock_ledger):
        """Returns 503 when MINT_STRIPE_CALLBACK_SECRET is not configured."""
        with patch("cashu.mint.callbacks.settings") as mock_settings:
            mock_settings.mint_stripe_callback_secret = None

            from fastapi import FastAPI

            from cashu.mint.callbacks import callback_router

            app = FastAPI()
            app.include_router(callback_router)
            client = TestClient(app)

            resp = client.post(
                "/v1/callbacks/stripe-payment",
                json={"checking_id": "abc-123"},
                headers={"Authorization": "Bearer some-token"},
            )
            assert resp.status_code == 503
            mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_missing_checking_id_body(self, client):
        """Missing checking_id in body returns 422."""
        resp = client.post(
            "/v1/callbacks/stripe-payment",
            json={},
            headers={"Authorization": "Bearer test-stripe-secret"},
        )
        assert resp.status_code == 422


class TestZBDCallback:
    """Tests for POST /v1/callbacks/zbd-payment."""

    def test_valid_bearer_token(self, client, mock_ledger):
        """Valid bearer token calls dispatcher and returns 200."""
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
            headers={"Authorization": "Bearer test-zbd-secret"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        mock_ledger.invoice_callback_dispatcher.assert_called_once_with("charge-456")

    def test_invalid_bearer_token(self, client, mock_ledger):
        """Invalid bearer token returns 401."""
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_missing_authorization_header(self, client, mock_ledger):
        """Missing Authorization header returns 401."""
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_missing_callback_secret_config(self, mock_ledger):
        """Returns 503 when MINT_ZBD_CALLBACK_SECRET is not configured."""
        with patch("cashu.mint.callbacks.settings") as mock_settings:
            mock_settings.mint_zbd_callback_secret = None

            from fastapi import FastAPI

            from cashu.mint.callbacks import callback_router

            app = FastAPI()
            app.include_router(callback_router)
            client = TestClient(app)

            resp = client.post(
                "/v1/callbacks/zbd-payment",
                json={"checking_id": "charge-456"},
                headers={"Authorization": "Bearer some-token"},
            )
            assert resp.status_code == 503
            mock_ledger.invoice_callback_dispatcher.assert_not_called()

    def test_stripe_token_doesnt_work_for_zbd(self, client, mock_ledger):
        """Stripe callback secret should not authenticate ZBD endpoint."""
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
            headers={"Authorization": "Bearer test-stripe-secret"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()
