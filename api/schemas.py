"""
api/schemas.py — Pydantic request/response models for the API.

All API input validation and output serialization goes through these
models. No raw dicts flow through the API layer.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from core.path_safety import looks_like_path_traversal, validate_repo_ref

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

_PR_REPO_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")


class CreateDebateRequest(BaseModel):
    """Body of POST /debates.

    pr_repo/pr_number/commit_sha and webhook_url are all optional and
    independent (GAP 17 / TASK 18) — a request with none of them set
    behaves exactly as it did before this feature existed. If provided,
    the completed debate's outcome is posted as a PR comment and/or a
    webhook POST — see core/notifications.py.
    """

    repo_ref: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Path or reference to the repo to review",
    )
    target_file: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Relative path to the file to patch",
    )
    ticket: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Description of the bug or feature to fix",
    )
    pr_repo: Optional[str] = Field(
        default=None,
        max_length=256,
        description="'owner/repo' — required together with pr_number to post a PR comment",
    )
    pr_number: Optional[int] = Field(
        default=None,
        gt=0,
        description="Pull request number — required together with pr_repo",
    )
    commit_sha: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional commit SHA the debate ran against, for reference only",
    )
    webhook_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="If set (or DEFAULT_WEBHOOK_URL is configured server-side), "
        "a JSON summary is POSTed here when the debate completes",
    )

    @field_validator("repo_ref")
    @classmethod
    def _validate_repo_ref_allowed(cls, v: str) -> str:
        """Fail-fast, authoritative check (GAP: repo_ref had NO validation
        at all — any authenticated caller could point Janus at an
        arbitrary filesystem path). See core/path_safety.py — this is the
        same allowlist check re-applied defensively in orchestrator.py
        before sandbox_copy(), not a check that exists only here."""
        try:
            validate_repo_ref(v)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return v

    @field_validator("target_file")
    @classmethod
    def _validate_target_file_not_obviously_malicious(cls, v: str) -> str:
        """Best-effort denylist pre-check (no sandbox exists yet at
        request-validation time, so this can't be the authoritative
        resolve()+is_relative_to() check — that happens in
        orchestrator.py once the sandbox path is known). Rejects the
        obvious cases (absolute paths, '..' components) fast, with a 422,
        instead of queuing a debate that fails deep in the worker."""
        if looks_like_path_traversal(v):
            raise ValueError(
                "target_file must be a relative path with no '..' components"
            )
        return v

    @field_validator("pr_repo")
    @classmethod
    def _validate_pr_repo_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _PR_REPO_PATTERN.match(v):
            raise ValueError("pr_repo must look like 'owner/repo'")
        return v

    @field_validator("webhook_url")
    @classmethod
    def _validate_webhook_scheme(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("webhook_url must start with http:// or https://")
        return v

    @model_validator(mode="after")
    def _pr_repo_and_number_together(self) -> "CreateDebateRequest":
        if (self.pr_repo is None) != (self.pr_number is None):
            raise ValueError("pr_repo and pr_number must be provided together, or not at all")
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CreateDebateResponse(BaseModel):
    """Response from POST /debates."""

    debate_id: str
    status: str


class GateCheckResult(BaseModel):
    check: str
    passed: bool
    detail: str = ""


class RoundResponse(BaseModel):
    """A single round within a debate."""

    round_num: int
    patch_text: str = ""
    reviewer_text: str = ""
    gate_result: Optional[dict[str, Any]] = None
    retrieved_example_ids: list[str] = Field(default_factory=list)
    repo_context_signals: dict[str, Any] = Field(default_factory=dict)
    stop_reason: Optional[str] = None
    code_extraction_failed: bool = False
    reviewer_skipped_counterexample: bool = False
    created_at: Optional[str] = None


class DebateResponse(BaseModel):
    """Full debate state returned by GET /debates/{id}."""

    id: str
    repo_ref: str
    target_file: str
    ticket: str
    status: str
    tenant_id: Optional[str] = None
    merged: Optional[bool] = None
    final_gate: Optional[dict[str, Any]] = None
    cost: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    pr_repo: Optional[str] = None
    pr_number: Optional[int] = None
    commit_sha: Optional[str] = None
    webhook_url: Optional[str] = None
    rounds: list[RoundResponse] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class HealthResponse(BaseModel):
    """Response from GET /healthz."""

    status: str
    db_reachable: bool
    sandbox_image_present: bool
    details: Optional[dict[str, str]] = None


class ErrorResponse(BaseModel):
    """Standard error response."""

    detail: str
