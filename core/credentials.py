"""Encrypted storage helpers for sensitive per-debate credentials."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from core.config import settings


def _fernet() -> Fernet:
    key = settings.BYOK_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "BYOK_ENCRYPTION_KEY must be configured before accepting request-level BYOK keys"
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("BYOK_ENCRYPTION_KEY must be a valid Fernet key") from exc


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError("Stored BYOK credential could not be decrypted") from exc
