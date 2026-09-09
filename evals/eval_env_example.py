"""Regression checks for the tracked, secret-free environment template."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"

EXPECTED_KEYS = {
    "API_KEYS",
    "ADMIN_API_KEYS",
    "ALLOWED_REPO_ROOTS",
    "CORS_ALLOWED_ORIGINS",
    "GOOGLE_API_KEY",
    "GOOGLE_API_KEYS",
    "GOOGLE_API_KEY_COOLDOWN_SECONDS",
    "ADV_REVIEW_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "NVIDIA_API_KEY",
    "COHERE_API_KEY",
    "OLLAMA_ENABLED",
    "OLLAMA_API_BASE",
    "BYOK_ENCRYPTION_KEY",
    "ADV_REVIEW_MAX_ROUNDS",
    "ADV_REVIEW_CIRCUIT_FAILURE_THRESHOLD",
    "ADV_REVIEW_CIRCUIT_COOLDOWN_SECONDS",
    "DATABASE_URL",
    "CHROMA_PERSIST_DIR",
    "CHROMA_COLLECTION",
    "SEED_DATA_PATH",
    "EMBEDDING_MODEL",
    "REPO_CONTEXT_MAX_FILES_SCANNED",
    "REPO_CONTEXT_MAX_PRIOR_FIXES",
    "REPO_CONTEXT_MAX_TEST_SAMPLES",
    "REPO_CONTEXT_SNIPPET_CHARS",
    "REPO_CONTEXT_GIT_TIMEOUT",
    "REPO_CONTEXT_FIX_KEYWORDS",
    "REPO_CONTEXT_TEST_DIR_NAMES",
    "USE_CONTAINERIZED_GATE",
    "SANDBOX_IMAGE",
    "SANDBOX_MEMORY_LIMIT",
    "SANDBOX_CPU_LIMIT",
    "SANDBOX_PID_LIMIT",
    "SANDBOX_TIMEOUT",
    "API_HOST",
    "API_PORT",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "WORKER_POLL_INTERVAL",
    "WORKER_MAX_CONCURRENT",
    "LLM_CALL_TIMEOUT_SECONDS",
    "ZOMBIE_SESSION_TIMEOUT_MINUTES",
    "ZOMBIE_SWEEP_INTERVAL_SECONDS",
    "LOG_LEVEL",
    "METRICS_ENABLED",
    "DIAGNOSTIC_PERSIST_TRACE",
    "DIAGNOSTIC_PERSIST_TRACE_PATH",
    "GITHUB_TOKEN",
    "GITHUB_API_URL",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET_REQUIRED",
    "DEFAULT_WEBHOOK_URL",
    "NOTIFICATION_TIMEOUT_SECONDS",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_JWT_TTL_SECONDS",
    "GITHUB_TOKEN_CACHE_SKEW_SECONDS",
    "GITHUB_REPO_CACHE_DIR",
}


def _defined_keys(text: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE)
    }


def test_env_example_exists_and_covers_runtime_surface():
    text = EXAMPLE.read_text(encoding="utf-8")
    keys = _defined_keys(text)

    assert keys >= EXPECTED_KEYS
    assert "DEPLOY_SSH_KEY=" not in text
    assert "-----BEGIN" not in text


def test_env_example_contains_only_safe_placeholders_for_secret_fields():
    text = EXAMPLE.read_text(encoding="utf-8")
    secret_lines = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in text.splitlines()
        if "=" in line
        and not line.lstrip().startswith("#")
        and line.split("=", 1)[0]
        in {
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GROQ_API_KEY",
            "NVIDIA_API_KEY",
            "COHERE_API_KEY",
            "BYOK_ENCRYPTION_KEY",
            "GITHUB_TOKEN",
            "GITHUB_WEBHOOK_SECRET",
            "GITHUB_APP_PRIVATE_KEY",
        }
    }

    assert secret_lines["GOOGLE_API_KEY"].startswith("replace-me")
    assert all(value == "" for key, value in secret_lines.items() if key != "GOOGLE_API_KEY")
    assert not re.search(r"(?:AIza|sk-[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,})", text)
