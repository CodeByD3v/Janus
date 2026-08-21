"""Tenant-isolated GitHub App installation-token handling."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx
import jwt

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)


class SecretStore(Protocol):
    """Secret-manager boundary; implementations must not persist secrets in DB."""

    def get(self, name: str, tenant_id: str | None = None) -> str | None:
        ...


class EnvironmentSecretStore:
    """Development adapter; production should replace this with a secret manager.

    The App private key is deliberately read only from the process environment
    and is never copied into tenant or installation database rows.
    """

    def get(self, name: str, tenant_id: str | None = None) -> str | None:
        if name == "github_app_private_key":
            return settings.GITHUB_APP_PRIVATE_KEY or None
        return None


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class GithubInstallationTokenProvider:
    """Mint and cache short-lived tokens scoped to one App installation."""

    def __init__(self, secret_store: SecretStore | None = None) -> None:
        self._secret_store = secret_store or EnvironmentSecretStore()
        self._cache: dict[tuple[str, int], _CachedToken] = {}
        self._lock = threading.RLock()

    def _app_jwt(self, tenant_id: str | None) -> str:
        if not settings.GITHUB_APP_ID:
            raise RuntimeError("GITHUB_APP_ID is required for installation tokens")
        private_key = self._secret_store.get("github_app_private_key", tenant_id)
        if not private_key:
            raise RuntimeError(
                "GitHub App private key is unavailable from the configured secret store"
            )
        private_key = private_key.replace("\\n", "\n")
        now = int(time.time())
        return str(
            jwt.encode(
                {
                    "iat": now - 60,
                    "exp": now + min(settings.GITHUB_APP_JWT_TTL_SECONDS, 600),
                    "iss": settings.GITHUB_APP_ID,
                },
                private_key,
                algorithm="RS256",
            )
        )

    def token_for(self, installation_id: int, tenant_id: str | None = None) -> str:
        if installation_id <= 0:
            raise ValueError("installation_id must be positive")
        cache_key = (tenant_id or "", installation_id)
        now = time.time()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached.expires_at - settings.GITHUB_TOKEN_CACHE_SKEW_SECONDS > now:
                return cached.token

        app_jwt = self._app_jwt(tenant_id)
        url = (
            f"{settings.GITHUB_API_URL.rstrip('/')}/app/installations/"
            f"{installation_id}/access_tokens"
        )
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
            if response.status_code >= 300:
                raise RuntimeError(
                    f"GitHub installation token request failed with status {response.status_code}"
                )
            payload = response.json()
            token = payload.get("token")
            expires_at_raw = payload.get("expires_at")
            if not isinstance(token, str) or not isinstance(expires_at_raw, str):
                raise RuntimeError("GitHub returned an incomplete installation token response")
            expires_at = datetime.fromisoformat(
                expires_at_raw.replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            logger.warning(
                "github_installation_token_failed",
                installation_id=installation_id,
                tenant_id=tenant_id,
                exc_info=True,
            )
            raise

        with self._lock:
            self._cache[cache_key] = _CachedToken(token=token, expires_at=expires_at)
        return token

    def headers_for(
        self,
        installation_id: int | None = None,
        tenant_id: str | None = None,
        legacy_token: str | None = None,
    ) -> dict[str, str] | None:
        if installation_id is not None:
            return {
                "Authorization": f"Bearer {self.token_for(installation_id, tenant_id)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        token = legacy_token if legacy_token is not None else settings.GITHUB_TOKEN
        if token:
            return {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        return None


_provider = GithubInstallationTokenProvider()


def github_headers(
    installation_id: int | None = None,
    tenant_id: str | None = None,
    legacy_token: str | None = None,
) -> dict[str, str] | None:
    """Resolve installation-scoped headers, with legacy static-token fallback."""
    return _provider.headers_for(installation_id, tenant_id, legacy_token)
