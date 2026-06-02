"""Tests for the fork-only Sentry module (cashu/mint/sentry.py).

These intentionally avoid importing sentry-sdk (which is installed only in the
Cloud Run image): the scrubber is a pure function and init_sentry() is a no-op
without SENTRY_DSN, so the whole module is exercisable in the standard test env.
"""

from cashu.mint import sentry


def test_scrub_sensitive_redacts_each_pattern():
    nsec = "nsec1" + "a" * 58
    preimage = "preimage: " + "c" * 64
    cases = {
        "pay cashuAeyJ0b2tlbiI6abc123_-X please": "[CASHU_TOKEN_REDACTED]",
        "invoice lnbc1pdeadbeef expired": "[LN_INVOICE_REDACTED]",
        "callback lnurl1dp68gurn8ghj7 broke": "[LNURL_REDACTED]",
        f"leaked {nsec} oops": "[NSEC_REDACTED]",
        f"{preimage} settled": "[PREIMAGE_REDACTED]",
    }
    for raw, placeholder in cases.items():
        out = sentry.scrub_sensitive(raw)
        assert placeholder in out
    # Original secrets must not survive.
    assert "cashuAeyJ0b2tlbiI6abc123_-X" not in sentry.scrub_sensitive(
        "cashuAeyJ0b2tlbiI6abc123_-X"
    )
    assert nsec not in sentry.scrub_sensitive(f"leaked {nsec}")


def test_scrub_sensitive_combined_and_benign():
    combined = "pay cashuBxyz via lnbc1abc key " + "nsec1" + "b" * 58
    out = sentry.scrub_sensitive(combined)
    assert "[CASHU_TOKEN_REDACTED]" in out
    assert "[LN_INVOICE_REDACTED]" in out
    assert "[NSEC_REDACTED]" in out

    benign = "ordinary error: connection refused"
    assert sentry.scrub_sensitive(benign) == benign
    assert sentry.scrub_sensitive("") == ""


def test_scrub_event_redacts_message_exception_breadcrumbs():
    event = {
        "message": "boot failed paying lnbc1deadbeef",
        "exception": {"values": [{"value": "panic with cashuAtok_en-123"}]},
        "breadcrumbs": {
            "values": [
                {"message": "request lnurl1dp68 received"},
                {"message": None},  # must not crash on non-str
            ]
        },
    }
    out = sentry._scrub_event(event, None)
    assert "[LN_INVOICE_REDACTED]" in out["message"]
    assert "[CASHU_TOKEN_REDACTED]" in out["exception"]["values"][0]["value"]
    assert "[LNURL_REDACTED]" in out["breadcrumbs"]["values"][0]["message"]


def test_init_sentry_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert sentry.init_sentry() is False


def test_init_sentry_noop_when_dsn_blank(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "   ")
    assert sentry.init_sentry() is False
