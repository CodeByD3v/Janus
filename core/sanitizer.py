"""
core/sanitizer.py — Redaction and sanitization utilities for agent text and API responses.

Ensures LLM prompts, API keys, credentials, and raw system instructions
are scrubbed before persisting or returning agent outputs to frontend callers.
"""

import re
from typing import Any

_SECRET_PATTERNS = [
    # Google API Keys
    re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"),
    # OpenAI API Keys
    re.compile(r"sk-[A-Za-z0-9_-]{32,}"),
    # Anthropic API Keys
    re.compile(r"sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{32,}"),
    # Generic Bearer Tokens
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
    # Generic Password / Key parameters in query strings or config
    re.compile(r"(api_key|apikey|secret|password|token)\s*=\s*['\"][^'\"]+['\"]", re.IGNORECASE),
]


def sanitize_text(text: str | None) -> str:
    """Sanitize agent output text by redacting secrets and sensitive credentials."""
    if not text:
        return ""

    sanitized = text
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED_SECRET]", sanitized)

    return sanitized


def sanitize_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    """Recursively sanitize string values inside a dictionary structure."""
    if not data:
        return {}

    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = sanitize_text(value)
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value)
        elif isinstance(value, list):
            result[key] = [
                sanitize_text(v) if isinstance(v, str)
                else sanitize_dict(v) if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            result[key] = value

    return result
