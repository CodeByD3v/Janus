"""Data models for the adversarial debate loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReviewerVerdict(str, Enum):
    """Structured verdict from the Reviewer agent.

    str enum so it JSON-serializes directly into DB fields and API
    responses without a custom encoder.
    """
    PASS = "PASS"               # Code is fine, no patcher needed
    ISSUE_FOUND = "ISSUE_FOUND" # Concrete bug, patcher must fix
    INCONCLUSIVE = "INCONCLUSIVE"  # Flag for human review


@dataclass
class RoundLog:
    round_num: int
    patch_text: str
    reviewer_text: str
    gate_result: dict[str, Any]
    reviewer_verdict: str = "ISSUE_FOUND"  # ReviewerVerdict value
    retrieved_example_ids: list[str] = field(default_factory=list)
    repo_context_signals: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    code_extraction_failed: bool = False
    reviewer_skipped_counterexample: bool = False


@dataclass
class DebateResult:
    merged: bool
    rounds: list[RoundLog] = field(default_factory=list)
    final_gate: dict[str, Any] | None = None
    sandbox_path: str | None = None
    cost: dict[str, Any] | None = None
    needs_human_review: bool = False  # True when any round was INCONCLUSIVE
    reviewer_verdict: str = "ISSUE_FOUND"  # Final verdict from last round
