"""Sentry error reporting for the Nutshell mint (Wavlake fork-only).

This is a fork-only module — it does not exist upstream. It is intentionally
self-contained and gated on the ``SENTRY_DSN`` environment variable so an
upstream ``cashubtc/nutshell`` merge has near-zero conflict surface: when the
DSN is unset (as it is upstream and in local/dev) ``init_sentry()`` is a no-op.

The ``sentry-sdk`` dependency is installed only in the Cloud Run image
(``Dockerfile.cloudrun``), NOT added to ``pyproject.toml`` / ``poetry.lock`` —
keeping the auto-generated lock file untouched avoids the worst class of
upstream-merge conflict. The import is therefore guarded: if ``sentry_sdk`` is
absent, this module degrades to a no-op rather than breaking imports/tests.

Mirrors the conventions used by the Go services (apps/api, apps/nwc-bridge):
service tag, environment gating, and scrubbing of Lightning/ecash secrets
before send. See docs/architecture/infra/WORKER_OBSERVABILITY.md.
"""

import os
import re

from loguru import logger

try:  # guarded: sentry-sdk is installed only in the Cloud Run image
    import sentry_sdk

    _SENTRY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where the dep is absent
    sentry_sdk = None  # type: ignore[assignment]
    _SENTRY_AVAILABLE = False

# service tag value — every event from this process is attributable to nutshell.
SERVICE_TAG = "nutshell"

# Patterns for scrubbing sensitive payment/ecash data from event text. Compiled
# once at import. Kept dependency-free (stdlib re only) so the scrubber is
# unit-testable without sentry-sdk installed.
_CASHU_TOKEN_RE = re.compile(r"\bcashu[AB][A-Za-z0-9_-]+\b")
_LN_INVOICE_RE = re.compile(r"\b(?:lnbc|lntb|lnbcrt)[a-z0-9]+\b", re.IGNORECASE)
_LNURL_RE = re.compile(r"\blnurl[a-z0-9]+\b", re.IGNORECASE)
_NSEC_RE = re.compile(r"\bnsec1[a-z0-9]{58}\b")
_PREIMAGE_RE = re.compile(r"\bpreimage[:\s]*[a-fA-F0-9]{64}\b", re.IGNORECASE)


def scrub_sensitive(text: str) -> str:
    """Redact Cashu tokens, Lightning invoices/LNURLs, nsec keys, and payment
    preimages from a string. Pure function — safe to call without sentry-sdk."""
    if not text:
        return text
    text = _NSEC_RE.sub("[NSEC_REDACTED]", text)
    text = _CASHU_TOKEN_RE.sub("[CASHU_TOKEN_REDACTED]", text)
    text = _LN_INVOICE_RE.sub("[LN_INVOICE_REDACTED]", text)
    text = _LNURL_RE.sub("[LNURL_REDACTED]", text)
    text = _PREIMAGE_RE.sub("[PREIMAGE_REDACTED]", text)
    return text


def _scrub_event(event, _hint):
    """before_send hook: scrub sensitive data from message, exceptions, and
    breadcrumbs. Returns the mutated event (never drops it)."""
    message = event.get("message")
    if isinstance(message, str):
        event["message"] = scrub_sensitive(message)

    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc.get("value"), str):
            exc["value"] = scrub_sensitive(exc["value"])

    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = scrub_sensitive(crumb["message"])

    return event


def _traces_sample_rate(environment: str) -> float:
    raw = os.environ.get("SENTRY_TRACES_SAMPLE_RATE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.25 if environment == "staging" else 0.1


def init_sentry() -> bool:
    """Initialize Sentry for the mint, gated on the ``SENTRY_DSN`` env var.

    No-op (returns ``False``) when the DSN is unset or ``sentry-sdk`` is not
    installed — so this is safe upstream, in tests, and in local/dev. Returns
    ``True`` when Sentry was initialized.

    Honored env vars: ``SENTRY_DSN`` (required to enable),
    ``SENTRY_ENVIRONMENT``/``ENVIRONMENT`` (default ``development``),
    ``SENTRY_RELEASE``, ``SENTRY_TRACES_SAMPLE_RATE``, ``SENTRY_SMOKE_TEST``.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry disabled (SENTRY_DSN not set)")
        return False
    if not _SENTRY_AVAILABLE:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; Sentry disabled")
        return False

    environment = os.environ.get(
        "SENTRY_ENVIRONMENT", os.environ.get("ENVIRONMENT", "development")
    )

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=os.environ.get("SENTRY_RELEASE") or None,
        traces_sample_rate=_traces_sample_rate(environment),
        attach_stacktrace=True,
        before_send=_scrub_event,
        server_name=os.environ.get("HOSTNAME") or None,
    )
    sentry_sdk.set_tag("service", SERVICE_TAG)
    logger.info(f"Sentry initialized (environment={environment}, service={SERVICE_TAG})")

    # Optional boot-time smoke test: when SENTRY_SMOKE_TEST is truthy and not in
    # production, emit a verified test event so an operator can confirm the
    # pipeline end-to-end after a deploy. Gated off in production.
    if os.environ.get("SENTRY_SMOKE_TEST", "").lower() in ("1", "true", "yes") and (
        environment != "production"
    ):
        sentry_sdk.capture_message(
            "nutshell sentry smoke-test message", level="info"
        )
        logger.info("Sentry smoke-test message emitted (SENTRY_SMOKE_TEST)")

    return True
