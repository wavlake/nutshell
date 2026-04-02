# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cashu Nutshell is a Chaumian Ecash wallet and mint implementation for Bitcoin Lightning, based on the Cashu protocol. It provides a CLI wallet (`cashu`) and a standalone mint server (`mint`). Written in Python 3.10+, managed with Poetry.

## Commands

```bash
# Install dependencies
poetry install
poetry install --with dev

# Run mint server
poetry run mint

# Run wallet CLI
poetry run cashu

# Run mint management CLI
poetry run mint-cli

# Formatting & linting
make format          # ruff autofix
make check           # ruff + mypy
make ruff            # ruff only
make mypy            # type checking only

# Tests (uses FakeWallet backend, SQLite by default)
make test            # all tests with coverage
make test-wallet     # wallet tests only
make test-mint       # mint tests only
pytest tests/mint/test_mint_api.py           # single file
pytest tests/mint/test_mint_api.py::test_fn  # single test

# Docker
make docker-build
docker compose up

# Pre-commit
make install-pre-commit-hook
```

Test environment is configured automatically: `DEBUG=true`, `PYTHONUNBUFFERED=1`, `MINT_BACKEND_BOLT11_SAT=FakeWallet`, `MINT_DATABASE=./test_data/test_mint`.

## Architecture

```
cashu/
├── core/           # Protocol layer: crypto (BDHKE, SECP, AES), data models, settings, DB abstraction
│   ├── nuts/       # NUT specification implementations
│   └── crypto/     # Blind Diffie-Hellman Key Exchange, proof verification
├── mint/           # Mint server (FastAPI + uvicorn)
│   ├── ledger.py   # Core mint logic (~53KB, largest file)
│   ├── router.py   # v1 API endpoints
│   ├── db/         # Read/write database operations
│   ├── auth/       # OIDC/Keycloak auth (NUT-21/22)
│   └── management_rpc/  # gRPC management interface
├── wallet/         # Wallet client
│   ├── wallet.py   # Main wallet logic (~62KB)
│   ├── cli/        # Click-based CLI
│   └── v1_api.py   # Wallet HTTP API
├── lightning/      # Pluggable Lightning backends (LND, CLN, Blink, Strike, LNbits, ZBD, FakeWallet)
└── tor/            # Optional Tor integration
```

**Key patterns:**
- Async-first (FastAPI + asyncio throughout)
- Settings via Pydantic settings hierarchy in `cashu/core/settings.py` (21 cascading classes, loaded from `.env` files)
- Database: SQLAlchemy 2.0 async — supports SQLite (aiosqlite) and PostgreSQL (asyncpg)
- Lightning backends implement abstract base in `cashu/lightning/base.py`
- Migrations are deterministic, split across `core/migrations.py`, `mint/migrations.py`, `wallet/migrations.py`
- FakeWallet (`cashu/lightning/fake.py`) simulates Lightning for testing — no real node needed

## Configuration

Settings are loaded from `.env` (cwd first, then `~/.cashu/.env`). See `.env.example` for all options. Key variables:

- `MINT_PRIVATE_KEY` — seed for key derivation
- `MINT_DATABASE` — SQLite path or PostgreSQL connection string
- `MINT_BACKEND_BOLT11_SAT` — Lightning backend selection (FakeWallet, LndRestWallet, CLNRestWallet, etc.)
- `MINT_LISTEN_HOST/PORT` — server binding (default 0.0.0.0:3338)
- `DEBUG=TRUE` + `LOG_LEVEL=TRACE` — verbose logging

## Fork: Upstream Merge Guide

This is a fork of [cashu/nutshell](https://github.com/cashubtc/nutshell). Minimizing upstream merge conflicts is a top priority. When syncing with upstream, these are the files we've modified and what each change does:

| File | What we changed | Merge notes |
|---|---|---|
| `cashu/core/base.py` | Added `backend: Optional[str]` field to `MintQuote` and its `from_row()` deserializer | If upstream adds fields to `MintQuote`, ours just needs to stay in the list |
| `cashu/mint/crud.py` | Added `backend` to the INSERT column list in `store_mint_quote()` | If upstream adds columns to this INSERT, just keep `backend` in both the column and VALUES lists |
| `cashu/mint/ledger.py` | `mint_quote()` and `get_mint_quote()` use `hasattr(wallet, "get_backend")` to dispatch to sub-backends | Highest-risk file. If upstream refactors these methods, re-apply the `hasattr` dispatch pattern around the `create_invoice` and `get_invoice_status` calls |
| `cashu/mint/router.py` | Extract `X-Cashu-Backend` header and pass `backend=` kwarg to `ledger.mint_quote()` | One-line addition in `mint_quote()` endpoint |
| `cashu/mint/migrations.py` | Added `m033_add_backend_to_mint_quotes` | If upstream adds m033, renumber ours. Migration is idempotent (catches duplicate column errors) |
| `cashu/mint/management_rpc/management_rpc.py` | `del mint_quote_dict['backend']` in `GetNut04Quote` | If upstream regenerates the proto with a `backend` field, remove this `del` |
| `cashu/lightning/__init__.py` | Added `ZBDStripeWallet` import | Append-only, low conflict risk |
| `tests/mint/test_mint_app_router.py` | Added `backend=None` to mock `mint_quote` signature | Keep in sync with `ledger.mint_quote()` signature |

Fork-only file (no conflict risk): `cashu/lightning/zbd_stripe.py`

## NUT Protocol

The codebase implements multiple NUT specifications (Notation, Usage & Terminology) for Cashu protocol features: core ecash operations, P2PK/HTLC spending conditions (NUT-10/14), DLEQ proofs (NUT-12), deterministic wallet backup (NUT-13), payment requests (NUT-18), caching (NUT-19), and authentication (NUT-21/22). Implementations live in `cashu/core/nuts/`.
