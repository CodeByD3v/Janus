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
    GOOGLE_API_KEY: str = field(default_factory=lambda: _optional("GOOGLE_API_KEY", ""))

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

    # --- Worker ---
    WORKER_POLL_INTERVAL: int = field(
        default_factory=lambda: _optional_int("WORKER_POLL_INTERVAL", 5)
    )
    WORKER_MAX_CONCURRENT: int = field(
        default_factory=lambda: _optional_int("WORKER_MAX_CONCURRENT", 4)
    )

    # --- Observability ---
    LOG_LEVEL: str = field(default_factory=lambda: _optional("LOG_LEVEL", "INFO"))
    METRICS_ENABLED: bool = field(
        default_factory=lambda: (
            _optional("METRICS_ENABLED", "true").lower() in ("true", "1", "yes")
        )
    )

    # --- MCP Server ---
    MCP_SERVER_SCRIPT: str = field(
        default_factory=lambda: _optional(
            "MCP_SERVER_SCRIPT",
            str(os.path.join(os.path.dirname(__file__), "mcp_server", "server.py")),
        )
    )

    def validate_for_api(self) -> None:
        """Validate settings required for the API server. Call at startup."""
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required for the API server")

    def validate_for_worker(self) -> None:
        """Validate settings required for the worker. Call at startup."""
        if not self.DATABASE_URL:
            raise ValueError("DATABASE_URL is required for the worker")
        if not self.GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY is required for the worker to call the LLM API")

    def validate_for_debate(self) -> None:
        """Validate settings required to run a debate."""
        if not self.GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY is required to run debates (set it via env var)")


# Singleton — constructed once at import time.
settings = Settings()
