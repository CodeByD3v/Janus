"""Pure-logic tests for the GitHub webhook integration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import github_app


def test_webhook_signature_verification():
    payload = b'{"action":"opened"}'
    secret = "test-secret"
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert github_app._verify_webhook_signature(payload, f"sha256={digest}", secret)
    assert not github_app._verify_webhook_signature(payload, "sha256=wrong", secret)
    assert not github_app._verify_webhook_signature(payload, "", secret)


def test_automatic_trigger_requires_janus_config(monkeypatch):
    config = "trigger: automatic\n"
    resource = {
        "encoding": "base64",
        "content": base64.b64encode(config.encode()).decode(),
    }
    monkeypatch.setattr(github_app, "_github_get", lambda *args, **kwargs: resource)
    assert github_app._repo_trigger_is_automatic("owner/repo", "abc123") is True

    manual_resource = {
        "encoding": "base64",
        "content": base64.b64encode(b"trigger: manual\n").decode(),
    }
    monkeypatch.setattr(github_app, "_github_get", lambda *args, **kwargs: manual_resource)
    assert github_app._repo_trigger_is_automatic("owner/repo", "abc123") is False


def test_primary_target_file_skips_traversal_and_unsupported_files(monkeypatch):
    files = [
        {"filename": "../../escape.py"},
        {"filename": "README.md"},
        {"filename": "src/review.ts"},
    ]
    monkeypatch.setattr(github_app, "_github_get", lambda *args, **kwargs: files)
    assert github_app._get_primary_target_file("owner/repo", 7) == "src/review.ts"
