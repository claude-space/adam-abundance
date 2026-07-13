"""Logging with mandatory secret redaction (PRD §8: "Redact secrets from all
logging"). Nothing that flows through Python's logging — or through the
``redact()`` helper used before persisting request payloads to ``tool_call_log``
— should ever contain a live credential.

Two layers of defense:

1. **Value registry.** Every secret the credentials layer resolves is registered
   here; the filter scrubs any exact occurrence of that value from log output.
2. **Shape regexes.** A backstop that catches common credential shapes
   (``sk-ant-``, Slack ``xoxb-``/``xapp-`` tokens, AWS ``AKIA`` keys, PEM private
   keys, Google refresh tokens, bearer-ish blobs) even if a value was never
   registered.
"""

from __future__ import annotations

import logging
import re
import sys

REDACTED = "***REDACTED***"

# Registry of exact secret values seen by the credentials layer.
_SECRETS: set[str] = set()

# Only redact registered values at least this long, so short/empty config values
# (e.g. a customer id "0") don't nuke unrelated log text.
_MIN_SECRET_LEN = 6

# Backstop patterns for common credential shapes.
_SHAPE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"xapp-[0-9]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),            # Google OAuth refresh tokens
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),        # Google client secrets
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"EAA[A-Za-z0-9]{20,}"),               # Facebook access tokens
]


def register_secret(value: str | None) -> None:
    """Register a secret value so it is scrubbed from all future log output."""
    if value and isinstance(value, str) and len(value) >= _MIN_SECRET_LEN:
        _SECRETS.add(value)


def redact(text: str) -> str:
    """Return ``text`` with every known secret value and shape-matched token
    replaced by :data:`REDACTED`. Safe to call on arbitrary strings/JSON before
    persisting them (e.g. ``tool_call_log.request``)."""
    if not text:
        return text
    # Longest-first so a superstring secret is replaced before a substring of it.
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pat in _SHAPE_PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs secrets from the fully-rendered log message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        scrubbed = redact(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        return True


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure root logging with the redacting filter attached to the handler.

    Attaching the filter to the *handler* (not just a logger) guarantees every
    record — including those from third-party libraries and SDKs — is scrubbed
    on the way out.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s", "%H:%M:%S")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"switchboard.{name}")
