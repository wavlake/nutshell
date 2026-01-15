#!/usr/bin/env python3
"""Integration test script for ZBD USD support.

This script validates the USD denomination support against the real ZBD sandbox API.
It tests:
1. Exchange rate fetching from ZBD API
2. USD cents to msats conversion accuracy
3. Invoice creation with USD amounts (requires MINT_ZBD_API_KEY)

Usage:
    # From monorepo root:
    cd services/nutshell
    poetry run python scripts/test_zbd_usd_integration.py

    # Or with task:
    task credits:test:zbd:integration

Environment Variables:
    MINT_ZBD_API_KEY: Required for invoice creation tests
    MINT_ZBD_ENDPOINT: Optional, defaults to https://api.zebedee.io
"""

import asyncio
import os
import sys

# Add the parent directory to the path so we can import cashu
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

# ANSI colors for output
GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"  # No Color


def print_header(text: str):
    print(f"\n{BLUE}{'=' * 60}{NC}")
    print(f"{BLUE}{text}{NC}")
    print(f"{BLUE}{'=' * 60}{NC}\n")


def print_pass(text: str):
    print(f"{GREEN}✅ PASS:{NC} {text}")


def print_fail(text: str):
    print(f"{RED}❌ FAIL:{NC} {text}")


def print_warn(text: str):
    print(f"{YELLOW}⚠️  WARN:{NC} {text}")


def print_info(text: str):
    print(f"{BLUE}ℹ️  INFO:{NC} {text}")


async def test_exchange_rate_fetch():
    """Test 1: Fetch exchange rate from real ZBD API."""
    print_header("Test 1: Exchange Rate Fetching")

    endpoint = os.getenv("MINT_ZBD_ENDPOINT", "https://api.zebedee.io")
    api_key = os.getenv("MINT_ZBD_API_KEY")

    if not api_key:
        print_warn("MINT_ZBD_API_KEY not set - using unauthenticated request")
        print_info("Exchange rate endpoint may require authentication")

    headers = {"apikey": api_key} if api_key else {}

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        try:
            r = await client.get(f"{endpoint}/v1/btcusd")
            r.raise_for_status()
            data = r.json()

            btc_usd_price = data.get("data", {}).get("btcUsdPrice")
            if btc_usd_price:
                rate = float(btc_usd_price)
                print_pass(f"Exchange rate fetched: ${rate:,.2f}/BTC")
                print_info(f"Full response: {data}")
                return rate
            else:
                print_fail(f"Unexpected response format: {data}")
                return None

        except httpx.HTTPStatusError as e:
            print_fail(f"HTTP error: {e.response.status_code} - {e.response.text}")
            return None
        except Exception as e:
            print_fail(f"Error fetching exchange rate: {e}")
            return None


def test_conversion_accuracy(rate: float):
    """Test 2: Validate USD cents to msats conversion."""
    print_header("Test 2: USD to msats Conversion Accuracy")

    def cents_to_msats(cents: int, btc_usd_rate: float) -> int:
        """Convert USD cents to millisatoshis (rounded up to whole sats)."""
        import math
        dollars = cents / 100
        btc = dollars / btc_usd_rate
        sats = btc * 100_000_000
        sats_rounded = math.ceil(sats)
        msats = sats_rounded * 1000
        return msats

    test_cases = [
        (1, "1 cent ($0.01)"),
        (10, "10 cents ($0.10)"),
        (100, "$1.00"),
        (1000, "$10.00"),
        (10000, "$100.00"),
    ]

    all_passed = True
    for cents, description in test_cases:
        msats = cents_to_msats(cents, rate)
        sats = msats / 1000
        dollars = cents / 100

        # Verify msats is divisible by 1000 (ZBD API requirement)
        if msats % 1000 != 0:
            print_fail(f"{description}: msats ({msats}) not divisible by 1000")
            all_passed = False
            continue

        # Verify the amount is within expected range (accounting for rounding up)
        # Calculate expected range: exact value to +1 sat
        exact_sats = (cents / 100 / rate) * 100_000_000
        min_expected = int(exact_sats) * 1000  # floor
        max_expected = (int(exact_sats) + 2) * 1000  # ceil + 1 sat tolerance

        if min_expected <= msats <= max_expected:
            print_pass(f"{description} = {msats:,} msats ({sats:,.0f} sats)")
        else:
            print_fail(f"{description}: {msats} msats outside expected range [{min_expected}, {max_expected}]")
            all_passed = False

    return all_passed


async def test_invoice_creation_usd(rate: float):
    """Test 3: Create a real USD invoice via ZBD API."""
    print_header("Test 3: USD Invoice Creation")

    api_key = os.getenv("MINT_ZBD_API_KEY")
    endpoint = os.getenv("MINT_ZBD_ENDPOINT", "https://api.zebedee.io")

    if not api_key:
        print_warn("MINT_ZBD_API_KEY not set - skipping invoice creation test")
        print_info("Set MINT_ZBD_API_KEY to test real invoice creation")
        return None

    # Convert $0.10 (10 cents) to msats (rounded up to whole sats)
    import math
    cents = 10
    dollars = cents / 100
    btc = dollars / rate
    sats = btc * 100_000_000
    sats_rounded = math.ceil(sats)
    msats = sats_rounded * 1000

    print_info(f"Creating invoice for {cents} cents (${dollars:.2f})")
    print_info(f"At rate ${rate:,.2f}/BTC = {msats:,} msats ({sats_rounded} sats, rounded up from {sats:.2f})")

    headers = {"apikey": api_key}
    payload = {
        "amount": str(msats),
        "description": "USD integration test - $0.10",
        "expiresIn": 300,  # 5 minutes
    }

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        try:
            r = await client.post(f"{endpoint}/v0/charges", json=payload)
            r.raise_for_status()
            data = r.json().get("data", {})

            charge_id = data.get("id")
            invoice = data.get("invoice", {}).get("request", "")[:50] + "..."
            status = data.get("status")

            if charge_id and invoice:
                print_pass(f"Invoice created successfully!")
                print_info(f"Charge ID: {charge_id}")
                print_info(f"Status: {status}")
                print_info(f"Invoice: {invoice}")
                return True
            else:
                print_fail(f"Unexpected response: {data}")
                return False

        except httpx.HTTPStatusError as e:
            print_fail(f"HTTP error: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            print_fail(f"Error creating invoice: {e}")
            return False


async def test_zbd_wallet_class():
    """Test 4: Test the actual ZBDWallet class with USD."""
    print_header("Test 4: ZBDWallet Class Integration")

    api_key = os.getenv("MINT_ZBD_API_KEY")
    if not api_key:
        print_warn("MINT_ZBD_API_KEY not set - skipping ZBDWallet class test")
        return None

    try:
        # Mock the settings since we're running standalone
        from unittest.mock import patch, MagicMock

        mock_settings = MagicMock()
        mock_settings.mint_zbd_api_key = api_key
        mock_settings.mint_zbd_endpoint = os.getenv(
            "MINT_ZBD_ENDPOINT", "https://api.zebedee.io"
        )
        mock_settings.mint_zbd_callback_url = ""
        mock_settings.mint_redis_url = None

        with patch("cashu.lightning.zbd.settings", mock_settings):
            from cashu.core.base import Amount, Unit
            from cashu.lightning.zbd import ZBDWallet

            # Create USD wallet instance
            wallet = ZBDWallet(unit=Unit.usd)
            print_pass("ZBDWallet created with Unit.usd")

            # Fetch exchange rate
            rate = await wallet.get_exchange_rate()
            print_pass(f"get_exchange_rate() returned ${rate:,.2f}/BTC")

            # Create invoice for $0.05 (5 cents)
            result = await wallet.create_invoice(
                amount=Amount(Unit.usd, 5),
                memo="ZBDWallet USD integration test",
            )

            if result.ok:
                print_pass("create_invoice() with USD succeeded!")
                print_info(f"Checking ID: {result.checking_id}")
                print_info(f"Invoice: {result.payment_request[:50]}...")
                return True
            else:
                print_fail(f"create_invoice() failed: {result.error_message}")
                return False

    except ImportError as e:
        print_fail(f"Import error: {e}")
        print_info("Make sure to run from services/nutshell with poetry")
        return False
    except Exception as e:
        print_fail(f"Error: {e}")
        return False


async def main():
    """Run all integration tests."""
    print("\n" + "=" * 60)
    print("  ZBD USD Support Integration Tests")
    print("=" * 60)

    results = {}

    # Test 1: Exchange rate fetch
    rate = await test_exchange_rate_fetch()
    results["exchange_rate"] = rate is not None

    if rate:
        # Test 2: Conversion accuracy
        results["conversion"] = test_conversion_accuracy(rate)

        # Test 3: Invoice creation (direct API)
        invoice_result = await test_invoice_creation_usd(rate)
        results["invoice_api"] = invoice_result

    # Test 4: ZBDWallet class
    wallet_result = await test_zbd_wallet_class()
    results["wallet_class"] = wallet_result

    # Summary
    print_header("Test Summary")

    passed = 0
    failed = 0
    skipped = 0

    for test_name, result in results.items():
        if result is True:
            print_pass(f"{test_name}")
            passed += 1
        elif result is False:
            print_fail(f"{test_name}")
            failed += 1
        else:
            print_warn(f"{test_name} (skipped)")
            skipped += 1

    print(f"\n{GREEN}Passed: {passed}{NC} | {RED}Failed: {failed}{NC} | {YELLOW}Skipped: {skipped}{NC}")

    if failed > 0:
        print(f"\n{RED}Some tests failed!{NC}")
        return 1
    elif passed == 0:
        print(f"\n{YELLOW}No tests ran - set MINT_ZBD_API_KEY for full coverage{NC}")
        return 0
    else:
        print(f"\n{GREEN}All tests passed!{NC}")
        return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
