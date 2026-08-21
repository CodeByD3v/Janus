"""Tests for GitHub App installation-token isolation."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import github_credentials
from core.config import settings


class _Store:
    def get(self, name: str, tenant_id: str | None = None) -> str | None:
        assert name == "github_app_private_key"
        assert tenant_id in ("tenant-a", "tenant-b")
        return "private-key"


class _Response:
    status_code = 201

    def json(self):
        return {"token": "installation-token", "expires_at": "2099-01-01T00:00:00Z"}


class _Client:
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        type(self).calls += 1
        return _Response()


def test_installation_token_is_cached_per_tenant_and_installation(monkeypatch):
    monkeypatch.setattr(
        github_credentials,
        "settings",
        replace(settings, GITHUB_APP_ID="123", GITHUB_APP_PRIVATE_KEY="unused"),
    )
    monkeypatch.setattr(github_credentials.httpx, "Client", _Client)
    monkeypatch.setattr(github_credentials.jwt, "encode", lambda *args, **kwargs: "app-jwt")
    _Client.calls = 0
    provider = github_credentials.GithubInstallationTokenProvider(_Store())

    assert provider.token_for(101, "tenant-a") == "installation-token"
    assert provider.token_for(101, "tenant-a") == "installation-token"
    assert provider.token_for(101, "tenant-b") == "installation-token"
    assert _Client.calls == 2


def test_missing_installation_uses_legacy_token_only_for_non_app_paths(monkeypatch):
    monkeypatch.setattr(
        github_credentials,
        "settings",
        replace(settings, GITHUB_TOKEN="legacy-token"),
    )
    headers = github_credentials.GithubInstallationTokenProvider().headers_for()
    assert headers is not None
    assert headers["Authorization"] == "Bearer legacy-token"
