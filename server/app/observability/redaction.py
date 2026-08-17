from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEYS = frozenset(
    {
        "audio",
        "body",
        "content",
        "image",
        "message",
        "payload",
        "prompt",
        "secret",
        "text",
        "token",
        "video",
    }
)


def redact_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Redact payload-shaped fields recursively before structured logging."""

    redacted: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SENSITIVE_KEYS:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, Mapping):
            redacted[key] = redact_fields(value)
        elif isinstance(value, list):
            redacted[key] = [
                redact_fields(item) if isinstance(item, Mapping) else item for item in value
            ]
        else:
            redacted[key] = value
    return redacted
