"""
config.py — centralized, environment-driven configuration.

Every module imports settings from here. No other module reads os.getenv
directly. Required secrets are validated at import time and fail fast with
a clear error if missing. Non-secret defaults (e.g. MAX_ROUNDS) have sane
values that can be overridden via environment variables.

Usage:
    from config import settings
    model = settings.MODEL
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Optional


def _require(name: str) -> str:
    """Read a required env var; crash immediately if missing."""
    value = os.environ.get(name)
    if not value:
        print(
            f"FATAL: required environment variable {name!r} is not set. "
            f"Set it before starting the service.",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def _optional(name: str, default: str) -> str:
    """Read an optional env var with a non-secret default."""
    return os.environ.get(name, default)


def _optional_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"WARNING: env var {name!r} has non-integer value {raw!r}, using default {default}",
            file=sys.stderr,
        )
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable settings container. Constructed once at import time."""

    # --- LLM ---
    MODEL: str = field(default_factory=lambda: _optional("ADV_REVIEW_MODEL", "gemini-2.5-flash"))
    # Singular kept working as a one-key fallback — existing deployments
    # that only set GOOGLE_API_KEY are unaffected by the pool (GAP 15).
    GOOGLE_API_KEY: str = field(default_factory=lambda: _optional("GOOGLE_API_KEY", ""))
    # Comma-separated list of keys for the KeyPool (core/llm_client.py).
    # If unset, google_api_keys() below falls back to [GOOGLE_API_KEY].
    GOOGLE_API_KEYS: str = field(default_factory=lambda: _optional("GOOGLE_API_KEYS", ""))
    GOOGLE_API_KEY_COOLDOWN_SECONDS: float = field(
        default_factory=lambda: float(_optional("GOOGLE_API_KEY_COOLDOWN_SECONDS", "30"))
    )

    # --- Debate ---
    MAX_ROUNDS: int = field(default_factory=lambda: _optional_int("ADV_REVIEW_MAX_ROUNDS", 5))
    APP_NAME: str = "adversarial_code_review"

    # --- Database ---
    DATABASE_URL: str = field(
        default_factory=lambda: _optional("DATABASE_URL", "sqlite:///./adversarial_code_review.db")
    )

    # --- Retrieval ---
    CHROMA_PERSIST_DIR: str = field(
        default_factory=lambda: _optional("CHROMA_PERSIST_DIR", "./chroma_store")
    )
    CHROMA_COLLECTION: str = field(
        default_factory=lambda: _optional("CHROMA_COLLECTION", "real_catch_examples")
    )
    SEED_DATA_PATH: str = field(
        default_factory=lambda: _optional(
            "SEED_DATA_PATH",
            str(os.path.join(os.path.dirname(__file__), "data", "real_catch_examples.seed.jsonl")),
        )
    )
    EMBEDDING_MODEL: str = field(
        default_factory=lambda: _optional("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    )

    # --- Repository-Context Retrieval (GAP 14) ---
    # Separate from the behavioral retrieval settings above — this tunes
    # core/repo_context.py's structural scan of the live sandboxed repo,
    # not the ChromaDB behavioral store.
    REPO_CONTEXT_MAX_FILES_SCANNED: int = field(
        default_factory=lambda: _optional_int("REPO_CONTEXT_MAX_FILES_SCANNED", 200)
    )
    REPO_CONTEXT_MAX_PRIOR_FIXES: int = field(
        default_factory=lambda: _optional_int("REPO_CONTEXT_MAX_PRIOR_FIXES", 5)
    )
    REPO_CONTEXT_MAX_TEST_SAMPLES: int = field(
        default_factory=lambda: _optional_int("REPO_CONTEXT_MAX_TEST_SAMPLES", 5)
    )
    REPO_CONTEXT_SNIPPET_CHARS: int = field(
        default_factory=lambda: _optional_int("REPO_CONTEXT_SNIPPET_CHARS", 400)
    )
    REPO_CONTEXT_GIT_TIMEOUT: int = field(
        default_factory=lambda: _optional_int("REPO_CONTEXT_GIT_TIMEOUT", 10)
    )
    REPO_CONTEXT_FIX_KEYWORDS: str = field(
        default_factory=lambda: _optional(
            "REPO_CONTEXT_FIX_KEYWORDS",
            "fix,bug,patch,issue,crash,regression,hotfix",
        )
    )
    # Common test-directory naming conventions, checked in order — the
    # first one that exists in the repo is used. Previously hardcoded to
    # "tests" only, which silently found zero samples on any repo using
    # a different (equally common) convention — verified concretely on a
    # real external repo, pytest-dev/pluggy, which uses "testing".
    REPO_CONTEXT_TEST_DIR_NAMES: str = field(
        default_factory=lambda: _optional(
            "REPO_CONTEXT_TEST_DIR_NAMES", "tests,testing,test"
        )
    )

    # --- Sandbox / Gate Execution ---
    SANDBOX_IMAGE: str = field(
        default_factory=lambda: _optional("SANDBOX_IMAGE", "adv-review-sandbox:latest")
    )
    SANDBOX_MEMORY_LIMIT: str = field(
        default_factory=lambda: _optional("SANDBOX_MEMORY_LIMIT", "512m")
    )
    SANDBOX_CPU_LIMIT: str = field(default_factory=lambda: _optional("SANDBOX_CPU_LIMIT", "1"))
    SANDBOX_PID_LIMIT: int = field(default_factory=lambda: _optional_int("SANDBOX_PID_LIMIT", 128))
    SANDBOX_TIMEOUT: int = field(default_factory=lambda: _optional_int("SANDBOX_TIMEOUT", 120))
    USE_CONTAINERIZED_GATE: bool = field(
        default_factory=lambda: (
            _optional("USE_CONTAINERIZED_GATE", "false").lower() in ("true", "1", "yes")
        )
    )

    # --- API ---
    API_HOST: str = field(default_factory=lambda: _optional("API_HOST", "0.0.0.0"))
    API_PORT: int = field(default_factory=lambda: _optional_int("API_PORT", 8000))
    RATE_LIMIT_REQUESTS: int = field(
        default_factory=lambda: _optional_int("RATE_LIMIT_REQUESTS", 60)
    )
    RATE_LIMIT_WINDOW_SECONDS: int = field(
        default_factory=lambda: _optional_int("RATE_LIMIT_WINDOW_SECONDS", 60)
    )
    # Comma-separated list of directories `repo_ref` is allowed to resolve
    # under. FAIL-CLOSED by design: empty means no repo_ref is accepted at
    # all, forcing an operator to explicitly opt in per deployment rather
    # than silently allowing arbitrary filesystem paths (see
    # api/schemas.py's repo_ref validator and core/path_safety.py).
    ALLOWED_REPO_ROOTS: str = field(
        default_factory=lambda: _optional("ALLOWED_REPO_ROOTS", "")
    )
    # Comma-separated allowed CORS origins, or "*" for any origin. Empty
    # disables CORS entirely (no cross-origin browser access). "*" is safe
    # here specifically because auth is header-based (X-API-Key), not
    # cookies — allow_credentials is always False, so there is nothing for
    # a malicious origin to ride on even with a wildcard.
    CORS_ALLOWED_ORIGINS: str = field(
        default_factory=lambda: _optional("CORS_ALLOWED_ORIGINS", "")
    )

    # --- Worker ---
    WORKER_POLL_INTERVAL: int = field(
        default_factory=lambda: _optional_int("WORKER_POLL_INTERVAL", 5)
    )
    WORKER_MAX_CONCURRENT: int = field(
        default_factory=lambda: _optional_int("WORKER_MAX_CONCURRENT", 4)
    )
    # A DebateSession stuck in status='running' with no activity (see
    # storage.db.sweep_zombie_sessions's docstring for exactly what
    # "activity" means) for longer than this is assumed to be a zombie
    # left behind by a crashed worker process, and is marked 'error'.
    ZOMBIE_SESSION_TIMEOUT_MINUTES: int = field(
        default_factory=lambda: _optional_int("ZOMBIE_SESSION_TIMEOUT_MINUTES", 30)
    )
    # How often the sweep runs, in seconds. Deliberately much less
    # frequent than WORKER_POLL_INTERVAL — this is a periodic
    # housekeeping pass over (usually zero) stuck sessions, not something
    # that needs to run every single poll cycle.
    ZOMBIE_SWEEP_INTERVAL_SECONDS: int = field(
        default_factory=lambda: _optional_int("ZOMBIE_SWEEP_INTERVAL_SECONDS", 300)
    )

    # --- Observability ---
    LOG_LEVEL: str = field(default_factory=lambda: _optional("LOG_LEVEL", "INFO"))
    METRICS_ENABLED: bool = field(
        default_factory=lambda: (
            _optional("METRICS_ENABLED", "true").lower() in ("true", "1", "yes")
        )
    )
    # Off by default. See ROADMAP.md §2 — a real, unresolved finding where
    # a persistence call appeared to not complete within the full worker
    # process after a failed LLM call sequence, and even
    # asyncio.wait_for's own timeout mechanism did not fire, suggesting
    # the event loop itself may stop servicing callbacks in that
    # scenario. When enabled, writes raw, synchronous, immediately-
    # fsync'd trace lines around persistence calls to
    # DIAGNOSTIC_PERSIST_TRACE_PATH, bypassing the logging framework and
    # asyncio entirely — deliberately the most primitive possible
    # instrumentation, so it still produces a signal even if the
    # suspected event-loop issue would otherwise prevent normal logging
    # from being observed. Meant to be turned on only while actively
    # reproducing that specific issue, in a persistent terminal — not for
    # routine use, and not equivalent to a stack-level tool like py-spy
    # (see scripts/reproduce_persist_hang.sh), but useful as a first,
    # zero-dependency signal.
    DIAGNOSTIC_PERSIST_TRACE: bool = field(
        default_factory=lambda: (
            _optional("DIAGNOSTIC_PERSIST_TRACE", "false").lower() in ("true", "1", "yes")
        )
    )
    DIAGNOSTIC_PERSIST_TRACE_PATH: str = field(
        default_factory=lambda: _optional(
            "DIAGNOSTIC_PERSIST_TRACE_PATH", "/tmp/janus_persist_trace.log"
        )
    )

    # --- Notifications (GAP 17) ---
    # All optional — a debate with no PR reference and no webhook configured
    # (neither globally here nor per-request) behaves exactly as it did
    # before this feature existed. See core/notifications.py.
    #
    # NOTE: these were missing from this file even though
    # core/notifications.py already depended on all four — any call to
    # post_github_pr_comment() or post_webhook() would have raised
    # AttributeError. Found and fixed while auditing this checkout.
    GITHUB_TOKEN: str = field(default_factory=lambda: _optional("GITHUB_TOKEN", ""))
    GITHUB_API_URL: str = field(
        default_factory=lambda: _optional("GITHUB_API_URL", "https://api.github.com")
    )
    # Fallback used only when a request doesn't set its own webhook_url.
    DEFAULT_WEBHOOK_URL: str = field(
        default_factory=lambda: _optional("DEFAULT_WEBHOOK_URL", "")
    )
    NOTIFICATION_TIMEOUT_SECONDS: int = field(
        default_factory=lambda: _optional_int("NOTIFICATION_TIMEOUT_SECONDS", 10)
    )

    # --- MCP Server ---
    MCP_SERVER_SCRIPT: str = field(
        default_factory=lambda: _optional(
            "MCP_SERVER_SCRIPT",
            str(os.path.join(os.path.dirname(__file__), "mcp_server", "server.py")),
        )
    )

    def allowed_repo_roots(self) -> list[str]:
        """Resolved list of directories repo_ref may live under. Empty
        list means fail-closed — no repo_ref is accepted until an
        operator explicitly configures this."""
        if not self.ALLOWED_REPO_ROOTS:
            return []
        return [r.strip() for r in self.ALLOWED_REPO_ROOTS.split(",") if r.strip()]

    def cors_origins(self) -> list[str]:
        """Resolved list of allowed CORS origins. Empty list means CORS
        is not enabled at all."""
        if not self.CORS_ALLOWED_ORIGINS:
            return []
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    def google_api_keys(self) -> list[str]:
        """Resolved list of keys for the KeyPool.

        GOOGLE_API_KEYS (comma-separated) takes precedence if set; falls
        back to a single-item list from GOOGLE_API_KEY; empty list if
        neither is configured.
        """
        if self.GOOGLE_API_KEYS:
            return [k.strip() for k in self.GOOGLE_API_KEYS.split(",") if k.strip()]
        if self.GOOGLE_API_KEY:
            return [self.GOOGLE_API_KEY]
        return []

    def validate_for_api(self) -> None:
        """Validate settings required for the API server. Call at startup."""
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required for the API server")

    def validate_for_worker(self) -> None:
        """Validate settings required for the worker. Call at startup."""
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required for the worker")
        if not self.google_api_keys():
            raise ValueError(
                "At least one Google API key is required for the worker to "
                "call the LLM API — set GOOGLE_API_KEYS (comma-separated) "
                "or GOOGLE_API_KEY"
            )

    def validate_for_debate(self) -> None:
        """Validate settings required to run a debate."""
        if not self.google_api_keys():
            raise ValueError(
                "At least one Google API key is required to run debates — "
                "set GOOGLE_API_KEYS (comma-separated) or GOOGLE_API_KEY"
            )


# Singleton — constructed once at import time.
settings = Settings()
