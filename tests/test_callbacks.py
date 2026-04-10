"""Unit tests for HTTP callback endpoints.

Tests cover:
- Valid bearer token marks quote paid (200)
- Invalid/missing bearer token returns 401
- Missing callback secret config returns 503
- Both stripe and zbd callback paths
- Dispatcher-level resolution: checking_id, request fallback, and loud
  failure on miss (regression coverage for the 2026-04-10 Stripe incident
  where a USD ZBD-created quote was silently dropped by the dispatcher
  because the Go API sent the bolt11 as checking_id).
"""

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from cashu.core.base import Amount, Method, MintQuoteState, Unit
from cashu.core.errors import CashuError
from cashu.core.models import PostMintQuoteRequest
from cashu.core.settings import settings
from cashu.lightning.base import InvoiceResponse
from cashu.mint.ledger import Ledger
from tests.helpers import is_postgres


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

    def test_dispatcher_error_returns_500(self, client, mock_ledger):
        """Dispatcher exception returns structured 500 JSON error."""
        mock_ledger.invoice_callback_dispatcher.side_effect = Exception("DB error")
        resp = client.post(
            "/v1/callbacks/stripe-payment",
            json={"checking_id": "abc-123"},
            headers={"Authorization": "Bearer test-stripe-secret"},
        )
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}


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

    def test_dispatcher_error_returns_500(self, client, mock_ledger):
        """Dispatcher exception returns structured 500 JSON error."""
        mock_ledger.invoice_callback_dispatcher.side_effect = Exception("DB error")
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
            headers={"Authorization": "Bearer test-zbd-secret"},
        )
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}

    def test_stripe_token_doesnt_work_for_zbd(self, client, mock_ledger):
        """Stripe callback secret should not authenticate ZBD endpoint."""
        resp = client.post(
            "/v1/callbacks/zbd-payment",
            json={"checking_id": "charge-456"},
            headers={"Authorization": "Bearer test-stripe-secret"},
        )
        assert resp.status_code == 401
        mock_ledger.invoice_callback_dispatcher.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatcher-level regression tests (real ledger + real crud, no HTTP mocks).
#
# These exercise cashu/mint/tasks.py::invoice_callback_dispatcher against a
# real Ledger with an in-memory-ish backend. They guard against the 2026-04-10
# incident where the dispatcher silently returned on a checking_id miss and
# a $11 Stripe-paid USD quote stayed UNPAID forever.
# ---------------------------------------------------------------------------


_FAKE_BOLT11 = (
    "lnbc1u1p3jz3vrpp5qdvyv0kqfylzj9m3d8w8l3x6e3n5qwk3fv5u8n4lp3v3q0h5p0zsdqqcqzpg"
    "xqyz5vqsp5fake1fake2fake3fake4fake5fake6fake7fake8fake9fake0fakeaq9qyyssq"
    "fakefakefakefakefakefakefakefakefakefakefakefakefakefakefakefakefakefakefa"
)


async def _fake_zbd_create_invoice(
    self,
    amount: Amount,
    memo=None,
    description_hash=None,
    unhashed_description=None,
) -> InvoiceResponse:
    """Deterministic ZBDWallet.create_invoice stub for tests.

    Returns a fixed ZBD-style checking_id (opaque charge UUID) and a
    fixed bolt11 payment_request, matching the production shape that
    triggered the incident.
    """
    return InvoiceResponse(
        ok=True,
        checking_id="zbd-charge-test-0001",
        payment_request=_FAKE_BOLT11,
    )


@pytest_asyncio.fixture
async def ledger_with_composite(monkeypatch, tmp_path):
    """Build an isolated Ledger with a real ZBDStripeWallet composite.

    Deliberately bypasses the conftest `ledger` fixture so this test does
    not race with the autouse session-scoped uvicorn `mint` server on the
    same sqlite file. Each run gets its own tmp sqlite directory.

    - Forces MINT_ZBD_API_KEY so ZBDWallet can initialize.
    - Monkeypatches ZBDWallet.create_invoice so no HTTP is made.
    - StripeWallet is used as-is (its create_invoice is pure in-process).
    """
    monkeypatch.setattr(settings, "mint_zbd_api_key", "test-zbd-api-key")
    # Ensure both sat and usd keysets get generated (conftest already sets
    # these at import time, but be explicit in case the module is imported
    # standalone).
    if not settings.mint_derivation_path_list:
        monkeypatch.setattr(
            settings, "mint_derivation_path_list", ["m/0'/2'/0'"]
        )

    from cashu.core.db import Database
    from cashu.core.migrations import migrate_databases
    from cashu.lightning.fake import FakeWallet
    from cashu.lightning.zbd import ZBDWallet
    from cashu.lightning.zbd_stripe import ZBDStripeWallet
    from cashu.mint import migrations as migrations_mint
    from cashu.mint.crud import LedgerCrudSqlite

    monkeypatch.setattr(ZBDWallet, "create_invoice", _fake_zbd_create_invoice)

    db_dir = tmp_path / "dispatcher_mint"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = Database("mint", str(db_dir))

    await migrate_databases(db, migrations_mint)

    composite = ZBDStripeWallet(unit=Unit.usd)
    backends = {
        Method.bolt11: {
            Unit.sat: FakeWallet(unit=Unit.sat),
            Unit.usd: composite,
        },
    }
    ledger = Ledger(
        db=db,
        seed=settings.mint_private_key or "TEST_PRIVATE_KEY",
        derivation_path=settings.mint_derivation_path or "m/0'/0'/0'",
        backends=backends,
        crud=LedgerCrudSqlite(),
    )
    # Activate both the sat keyset (from mint_derivation_path) and the usd
    # keyset (from mint_derivation_path_list) — same flow as _startup_keysets
    # but without the _check_backends / listener / watchdog side effects
    # which would require a working backend balance.
    await ledger._startup_keysets()
    yield ledger
    await db.engine.dispose()


class TestDispatcherLookup:
    """Tests against invoice_callback_dispatcher with a real ledger + crud."""

    @pytest.mark.asyncio
    async def test_stripe_quote_resolved_by_checking_id(
        self, ledger_with_composite: Ledger
    ):
        """Happy path: caller sends the StripeWallet-generated UUID."""
        quote = await ledger_with_composite.mint_quote(
            PostMintQuoteRequest(unit="usd", amount=1100),
            backend="stripe",
        )
        # StripeWallet.create_invoice returns a uuid checking_id and
        # "stripe:<uuid>" payment_request.
        assert quote.request.startswith("stripe:")
        assert quote.state == MintQuoteState.unpaid

        await ledger_with_composite.invoice_callback_dispatcher(quote.checking_id)

        refreshed = await ledger_with_composite.crud.get_mint_quote(
            quote_id=quote.quote, db=ledger_with_composite.db
        )
        assert refreshed is not None
        assert refreshed.state == MintQuoteState.paid
        assert refreshed.paid_time is not None

    @pytest.mark.asyncio
    async def test_stripe_quote_resolved_by_request_fallback(
        self, ledger_with_composite: Ledger
    ):
        """Fallback: caller sends the payment_request ('stripe:<uuid>')."""
        quote = await ledger_with_composite.mint_quote(
            PostMintQuoteRequest(unit="usd", amount=1100),
            backend="stripe",
        )

        await ledger_with_composite.invoice_callback_dispatcher(quote.request)

        refreshed = await ledger_with_composite.crud.get_mint_quote(
            quote_id=quote.quote, db=ledger_with_composite.db
        )
        assert refreshed is not None
        assert refreshed.state == MintQuoteState.paid

    @pytest.mark.asyncio
    async def test_zbd_quote_resolved_by_bolt11_fallback(
        self, ledger_with_composite: Ledger
    ):
        """The 2026-04-10 incident: ZBD USD quote + bolt11 as checking_id.

        The composite defaults to ZBD when no X-Cashu-Backend is provided.
        ZBDWallet stores a charge id as checking_id and the bolt11 as
        request. Before this patch the dispatcher looked up by checking_id
        only, missed, and silently returned — the quote stayed UNPAID.
        With the fallback, the dispatcher resolves by request and marks it
        paid.
        """
        quote = await ledger_with_composite.mint_quote(
            PostMintQuoteRequest(unit="usd", amount=1100),
        )
        # Sanity: the monkeypatched ZBD stub put the bolt11 in request and
        # an opaque charge id in checking_id (production shape).
        assert quote.checking_id == "zbd-charge-test-0001"
        assert quote.request == _FAKE_BOLT11.lower()

        # Go API incorrectly (historically) sends the bolt11 as checking_id.
        await ledger_with_composite.invoice_callback_dispatcher(_FAKE_BOLT11)

        refreshed = await ledger_with_composite.crud.get_mint_quote(
            quote_id=quote.quote, db=ledger_with_composite.db
        )
        assert refreshed is not None
        assert refreshed.state == MintQuoteState.paid

    @pytest.mark.asyncio
    async def test_dispatcher_idempotent_on_already_paid(
        self, ledger_with_composite: Ledger
    ):
        """Repeated callbacks do not crash or double-transition."""
        quote = await ledger_with_composite.mint_quote(
            PostMintQuoteRequest(unit="usd", amount=1100),
            backend="stripe",
        )
        await ledger_with_composite.invoice_callback_dispatcher(quote.checking_id)
        # Second call must succeed and leave state paid.
        await ledger_with_composite.invoice_callback_dispatcher(quote.checking_id)

        refreshed = await ledger_with_composite.crud.get_mint_quote(
            quote_id=quote.quote, db=ledger_with_composite.db
        )
        assert refreshed is not None
        assert refreshed.state == MintQuoteState.paid

    @pytest.mark.asyncio
    async def test_unknown_checking_id_raises(
        self, ledger_with_composite: Ledger
    ):
        """Unknown identifier must raise CashuError (not silently return).

        This is the key guard: the prior dispatcher did `logger.error; return`,
        which the callback endpoint turned into 200 OK and masked the bug.
        """
        with pytest.raises(CashuError, match="Quote not found"):
            await ledger_with_composite.invoice_callback_dispatcher(
                "never-seen-me"
            )

    @pytest.mark.asyncio
    async def test_endpoint_returns_500_on_miss(
        self, ledger_with_composite: Ledger
    ):
        """Integration: the callback HTTP endpoint surfaces the dispatcher
        failure as 500 instead of the previous silent 200.

        Uses httpx.AsyncClient with ASGITransport so the request runs on the
        same event loop as the ledger fixture (the sync TestClient would spin
        up a worker thread and break the fixture's asyncio sqlite connection).
        """
        import httpx
        from fastapi import FastAPI

        from cashu.mint.callbacks import callback_router

        app = FastAPI()
        app.include_router(callback_router)

        with patch("cashu.mint.callbacks.ledger", ledger_with_composite), patch(
            "cashu.mint.callbacks.settings"
        ) as mock_settings:
            mock_settings.mint_stripe_callback_secret = "test-secret"
            mock_settings.mint_zbd_callback_secret = "test-secret"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                resp = await client.post(
                    "/v1/callbacks/stripe-payment",
                    json={"checking_id": "does-not-exist"},
                    headers={"Authorization": "Bearer test-secret"},
                )
            assert resp.status_code == 500
            assert resp.json() == {"detail": "Internal error"}

    @pytest.mark.asyncio
    async def test_checking_id_lookup_takes_priority_over_request(
        self, ledger_with_composite: Ledger
    ):
        """If the same string is a valid checking_id for quote A AND the
        request for quote B, the primary (checking_id) lookup must win —
        the fallback should only run on a miss. Pins the resolution order
        so a future refactor can't silently flip priorities.
        """
        import time

        from cashu.core.base import MintQuote

        target = await ledger_with_composite.mint_quote(
            PostMintQuoteRequest(unit="usd", amount=1100),
            backend="stripe",
        )

        # Hand-insert a decoy quote whose `request` collides with target's
        # checking_id. Using the crud directly avoids a second real mint_quote
        # call (which would generate its own Stripe uuid).
        decoy = MintQuote(
            quote="decoy-quote-id",
            method="bolt11",
            request=target.checking_id,
            checking_id="decoy-checking-id",
            unit="usd",
            amount=1100,
            state=MintQuoteState.unpaid,
            created_time=int(time.time()),
        )
        await ledger_with_composite.crud.store_mint_quote(
            quote=decoy, db=ledger_with_composite.db
        )

        await ledger_with_composite.invoice_callback_dispatcher(
            target.checking_id
        )

        refreshed_target = await ledger_with_composite.crud.get_mint_quote(
            quote_id=target.quote, db=ledger_with_composite.db
        )
        refreshed_decoy = await ledger_with_composite.crud.get_mint_quote(
            quote_id="decoy-quote-id", db=ledger_with_composite.db
        )
        assert refreshed_target is not None
        assert refreshed_decoy is not None
        assert refreshed_target.state == MintQuoteState.paid
        assert refreshed_decoy.state == MintQuoteState.unpaid


@pytest.mark.asyncio
@pytest.mark.skipif(
    not is_postgres,
    reason="SQLite lock_select_statement is ignored; needs Postgres FOR UPDATE NOWAIT",
)
async def test_dispatcher_row_lock_under_postgres(
    ledger_with_composite: Ledger,
):
    """Exercise the dispatcher's FOR UPDATE NOWAIT path against Postgres.

    Starts the dispatcher, and while it holds the quote row lock (simulated
    by holding a concurrent connection that's locked on the same row), the
    dispatcher should still complete because `quote = :quote` is the same
    single-column pattern ledger.get_mint_quote() uses. If a future refactor
    moves back to a compound WHERE clause, this test will catch any
    driver-level incompatibility with asyncpg + SQLAlchemy text().
    """
    import asyncio

    quote = await ledger_with_composite.mint_quote(
        PostMintQuoteRequest(unit="usd", amount=1100),
        backend="stripe",
    )

    dispatcher_started = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_competing_lock():
        """Acquire the same row lock and hold it briefly."""
        async with ledger_with_composite.db.get_connection(
            lock_table="mint_quotes",
            lock_select_statement="quote = :quote",
            lock_parameters={"quote": quote.quote},
            lock_timeout=5,
        ):
            dispatcher_started.set()
            await release_holder.wait()

    async def run_dispatcher():
        # Wait until the competing lock is held, then call dispatcher.
        # It should block on the lock, then succeed once released.
        await dispatcher_started.wait()
        # Schedule release after a short delay.
        loop = asyncio.get_event_loop()
        loop.call_later(0.2, release_holder.set)
        await ledger_with_composite.invoice_callback_dispatcher(quote.checking_id)

    await asyncio.gather(hold_competing_lock(), run_dispatcher())

    refreshed = await ledger_with_composite.crud.get_mint_quote(
        quote_id=quote.quote, db=ledger_with_composite.db
    )
    assert refreshed is not None
    assert refreshed.state == MintQuoteState.paid
