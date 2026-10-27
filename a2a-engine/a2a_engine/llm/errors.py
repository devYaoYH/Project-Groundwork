"""Bounded provider diagnostics suitable for owner-visible logs and traces."""
from __future__ import annotations

import json
import re
from urllib.error import HTTPError

_MAX_BODY_BYTES = 16 * 1024
_MAX_MESSAGE_CHARS = 1000


def provider_error_detail(exc: BaseException, *, api_key: str = "") -> str | None:
    """Extract only a JSON error message, never a raw response or request echo.

    Provider metadata may contain the entire submitted multimodal request. Read
    a bounded body and allow only the conventional message field through. HTML,
    malformed/oversized bodies and failures to read remain safely omitted.
    """
    if not isinstance(exc, HTTPError):
        return None
    try:
        raw = exc.read(_MAX_BODY_BYTES + 1)
        if len(raw) > _MAX_BODY_BYTES:
            return None
        body = json.loads(raw)
        if not isinstance(body, dict):
            return None
        error = body.get("error", body)
        message = error.get("message") if isinstance(error, dict) else error
        if not isinstance(message, str):
            return None
        if api_key:
            message = message.replace(api_key, "[redacted credential]")
        # Even message fields can echo auth headers, private URLs or image data.
        message = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", message)
        message = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted credential]", message)
        message = re.sub(r"(?i)data:[^\s\"<>]+", "[redacted image]", message)
        message = re.sub(r"(?i)https?://[^\s\"<>]+", "[redacted URL]", message)
        message = re.sub(r"[A-Za-z0-9_+/=-]{64,}", "[redacted data]", message)
        # Keep each diagnostic on one line; remove terminal/control characters.
        message = " ".join("".join(c if c.isprintable() else " " for c in message).split())
        if len(message) > _MAX_MESSAGE_CHARS:
            suffix = " [truncated]"
            message = message[:_MAX_MESSAGE_CHARS - len(suffix)] + suffix
        return message or None
    except Exception:
        # Diagnostics must never mask the provider failure or change retries.
        return None
