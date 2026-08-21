"""
api/auth.py — API key authentication and per-key rate limiting.

Design:
- API keys are passed via the X-API-Key header
- Keys are hashed at rest (SHA-256) and mapped to a tenant/caller id
- Per-key rate limiting uses a token bucket (in-memory for single-instance,
  pluggable interface for Redis when scaling horizontally)
- No plaintext keys in code, config, or logs

Usage:
    from api.auth import require_api_key
    @app.get("/endpoint")
    async def endpoint(tenant: str = Depends(require_api_key)):
        ...
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# Key store — maps hashed API keys to tenant IDs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _KeyMetadata:
    tenant_id: str
    role: str


class KeyStore:
    """In-memory store of hashed API keys -> tenant metadata.

    In production, this would be backed by the DB or a secrets manager.
    For the first cut, keys are loaded from the API_KEYS env var
    (format: "key1:tenant1,key2:tenant2") and hashed at rest.
    """

    def __init__(self) -> None:
        self._keys: dict[str, _KeyMetadata] = {}  # hash -> metadata
        self._lock = threading.Lock()

    def register_key(self, raw_key: str, tenant_id: str, role: str = "tenant") -> None:
        """Register a raw API key, storing only its hash and role."""
        if not raw_key or role not in {"tenant", "admin"}:
            return
        key_hash = self._hash_key(raw_key)
        with self._lock:
            existing = self._keys.get(key_hash)
            if existing is not None and existing.role != role:
                # A credential must never straddle privilege domains. Remove
                # the collision rather than silently granting either role.
                self._keys.pop(key_hash, None)
                logger.error("api_key_role_conflict")
                return
            self._keys[key_hash] = _KeyMetadata(tenant_id=tenant_id, role=role)
        logger.info("api_key_registered", tenant_id=tenant_id, role=role)

    def validate_key(self, raw_key: str) -> Optional[str]:
        """Validate a tenant key, excluding credentials with the admin role."""
        metadata = self._metadata_for(raw_key)
        if metadata is None or metadata.role != "tenant":
            return None
        return metadata.tenant_id

    def validate_admin_key(self, raw_key: str) -> Optional[str]:
        """Validate an admin key. Returns its operator id if authorized."""
        metadata = self._metadata_for(raw_key)
        if metadata is None or metadata.role != "admin":
            return None
        return metadata.tenant_id

    def _metadata_for(self, raw_key: str) -> _KeyMetadata | None:
        key_hash = self._hash_key(raw_key)
        with self._lock:
            return self._keys.get(key_hash)

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def load_from_env(self) -> None:
        """Load API keys from the API_KEYS env var.

        Format: "key1:tenant1,key2:tenant2"
        """
        raw = os.environ.get("API_KEYS", "")
        if not raw:
            logger.warning(
                "no_api_keys_configured",
                detail="API_KEYS env var is empty — tenant requests will be rejected. "
                "Set API_KEYS=key1:tenant1,key2:tenant2 to enable access.",
            )
        else:
            for pair in raw.split(","):
                pair = pair.strip()
                if ":" not in pair:
                    logger.warning("invalid_api_key_entry", entry="***")
                    continue
                key, tenant = pair.split(":", 1)
                self.register_key(key.strip(), tenant.strip(), role="tenant")

        admin_raw = settings.ADMIN_API_KEYS
        for pair in admin_raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if ":" not in pair:
                logger.warning("invalid_admin_api_key_entry", entry="***")
                continue
            key, operator = pair.split(":", 1)
            self.register_key(key.strip(), operator.strip(), role="admin")


# Global key store — loaded once at startup
key_store = KeyStore()


# ---------------------------------------------------------------------------
# Rate limiter — token bucket per API key
# ---------------------------------------------------------------------------

class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, max_tokens: int, refill_period: float) -> None:
        self.max_tokens = max_tokens
        self.refill_period = refill_period
        self._tokens: float = float(max_tokens)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one token. Returns True if allowed, False if rate limited."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            # Refill tokens based on time elapsed
            self._tokens = min(
                self.max_tokens,
                self._tokens + (elapsed / self.refill_period) * self.max_tokens,
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class RateLimiter:
    """Per-key rate limiter using token buckets.

    Pluggable: this in-memory implementation is suitable for single-instance
    deployment. Replace with a Redis-backed implementation for horizontal
    scaling by swapping this class (same interface).
    """

    def __init__(
        self,
        max_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self.max_requests = max_requests or settings.RATE_LIMIT_REQUESTS
        self.window_seconds = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Check if a request from this key is allowed."""
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(
                    self.max_requests, float(self.window_seconds)
                )
            return self._buckets[key].consume()


# Global rate limiter
rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def require_api_key(
    request: Request,
    api_key: Optional[str] = Security(_api_key_header),
) -> str:
    """FastAPI dependency that validates the API key and rate limit.

    Returns the tenant_id if the key is valid and not rate limited.
    Raises HTTPException otherwise.
    """
    if not api_key:
        logger.warning("auth_missing_key", path=request.url.path)
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    tenant_id = key_store.validate_key(api_key)
    if tenant_id is None:
        logger.warning("auth_invalid_key", path=request.url.path)
        raise HTTPException(status_code=401, detail="Invalid API key")

    if not rate_limiter.check(tenant_id):
        logger.warning("rate_limited", tenant_id=tenant_id, path=request.url.path)
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return tenant_id


async def require_admin_api_key(
    request: Request,
    api_key: Optional[str] = Security(_api_key_header),
) -> str:
    """Require a key explicitly registered with the admin role."""
    if not api_key:
        logger.warning("admin_auth_missing_key", path=request.url.path)
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    operator_id = key_store.validate_admin_key(api_key)
    if operator_id is None:
        logger.warning("admin_auth_forbidden", path=request.url.path)
        raise HTTPException(status_code=403, detail="Admin authorization required")

    if not rate_limiter.check(f"admin:{operator_id}"):
        logger.warning("admin_rate_limited", path=request.url.path)
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    return operator_id
