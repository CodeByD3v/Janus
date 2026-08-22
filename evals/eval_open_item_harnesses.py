"""Regression tests for operational harnesses added for remaining open items."""

from __future__ import annotations

import json
import sys

from api.github_app import _verify_webhook_signature
from scripts.smoke_github_app import _signature, main


def test_github_smoke_signature_matches_webhook_verifier():
    payload = b'{"action":"opened"}'
    secret = "smoke-secret"
    signature = _signature(payload, secret)

    assert _verify_webhook_signature(payload, signature, secret) is True


def test_github_smoke_refuses_network_without_explicit_confirmation(monkeypatch, tmp_path, capsys):
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"action": "opened"}), encoding="utf-8")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "smoke-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke_github_app.py", "https://example.invalid/github/webhook", str(payload_path)],
    )

    assert main() == 2
    assert "--confirm-live" in capsys.readouterr().err


def test_github_smoke_rejects_missing_secret_before_network(monkeypatch, tmp_path, capsys):
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"action": "opened"}), encoding="utf-8")
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke_github_app.py", "https://example.invalid/github/webhook", str(payload_path), "--confirm-live"],
    )

    assert main() == 2
    assert "GITHUB_WEBHOOK_SECRET" in capsys.readouterr().err
