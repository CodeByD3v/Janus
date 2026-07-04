"""
api/schemas.py — Pydantic request/response models for the API.

All API input validation and output serialization goes through these
models. No raw dicts flow through the API layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateDebateRequest(BaseModel):
    """Body of POST /debates."""
    repo_ref: str = Field(
        ..., min_length=1, max_length=512,
        description="Path or reference to the repo to review",
    )
    target_file: str = Field(
        ..., min_length=1, max_length=512,
        description="Relative path to the file to patch",
    )
    ticket: str = Field(
        ..., min_length=1, max_length=4096,
        description="Description of the bug or feature to fix",
    )


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
