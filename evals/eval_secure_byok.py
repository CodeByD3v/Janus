"""Tests for encrypted request-level BYOK credential handling."""

from __future__ import annotations

from dataclasses import replace
import sys
from pathlib import Path

from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import credentials
from core.config import settings


def test_byok_round_trip_is_encrypted(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(credentials, "settings", replace(settings, BYOK_ENCRYPTION_KEY=key))
    encrypted = credentials.encrypt_secret("super-secret-provider-key")
    assert encrypted != "super-secret-provider-key"
    assert credentials.decrypt_secret(encrypted) == "super-secret-provider-key"


def test_byok_requires_encryption_key(monkeypatch):
    monkeypatch.setattr(credentials, "settings", replace(settings, BYOK_ENCRYPTION_KEY=""))
    try:
        credentials.encrypt_secret("secret")
    except RuntimeError as exc:
        assert "BYOK_ENCRYPTION_KEY" in str(exc)
    else:
        raise AssertionError("missing BYOK encryption key must fail closed")
