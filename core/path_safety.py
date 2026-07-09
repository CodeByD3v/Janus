"""
path_safety.py — shared path-traversal defenses for repo_ref and target_file.

Two separate concerns, both handled here so the validation logic lives in
exactly one place instead of being reimplemented (and potentially
reimplemented inconsistently) at each call site:
S
1. `repo_ref` (which local directory gets copied into a sandbox at all)
   must resolve under one of settings.ALLOWED_REPO_ROOTS. This is an
   ALLOWLIST check — fail-closed by design, since an unrestricted
   repo_ref lets any authenticated caller point Janus at an arbitrary
   filesystem path (e.g. `/etc`, `/home/deploy/.ssh`) and have its
   contents copied into a sandbox, handed to two LLM agents, and
   potentially surfaced back through the API response or a GAP 17
   webhook/PR comment.

2. `target_file` (which file inside that sandbox gets read/written) must
   resolve inside the sandbox root. Enforced authoritatively in
   orchestrator.py using resolve() + is_relative_to() once the sandbox
   path is known — the check here is a best-effort DENYLIST pre-check at
   request-validation time, before the sandbox exists, so obviously
   malicious input gets a fast 422 instead of silently queuing a debate
   that fails deep in the worker. It is defense-in-depth, not a
   replacement for the authoritative check.

Every function here raises ValueError with a message safe to show to an
API caller (no internal paths, no stack traces) — callers decide whether
that becomes a 422 (api/schemas.py) or a failed DebateResult
(orchestrator.py).
"""

from __future__ import annotations

from pathlib import Path

from core.config import settings


def validate_repo_ref(repo_ref: str) -> str:
    """Validate that repo_ref resolves under one of the configured
    allowed roots. Returns repo_ref unchanged if valid; raises
    ValueError otherwise.

    Fail-closed: if ALLOWED_REPO_ROOTS isn't configured, every repo_ref
    is rejected, not silently allowed.
    """
    allowed_roots = settings.allowed_repo_roots()
    if not allowed_roots:
        raise ValueError(
            "repo_ref is not permitted: no ALLOWED_REPO_ROOTS configured "
            "on this server"
        )

    try:
        candidate = Path(repo_ref).resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"repo_ref could not be resolved: {e}") from None

    for root in allowed_roots:
        root_resolved = Path(root).resolve()
        if candidate.is_relative_to(root_resolved):
            return repo_ref

    raise ValueError(
        "repo_ref must resolve under one of the server's configured "
        "allowed repository roots"
    )


def looks_like_path_traversal(target_file: str) -> bool:
    """Best-effort denylist check for target_file, usable BEFORE a
    sandbox exists (so before we have a base path to resolve() against
    authoritatively). Returns True if the value looks unsafe.

    This is intentionally conservative — it flags absolute paths and any
    '..' path component, which covers realistic misuse without needing
    to know the sandbox root. It does NOT replace the resolve() +
    is_relative_to() check in orchestrator.py, which is the authoritative
    check once the actual sandbox path is known.
    """
    if not target_file or not target_file.strip():
        return True

    candidate = Path(target_file)
    if candidate.is_absolute():
        return True
    if ".." in candidate.parts:
        return True

    return False
