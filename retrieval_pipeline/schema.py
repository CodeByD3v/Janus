"""
schema.py — Pydantic model for 'real catch' example records.

Each record represents a real-world code review finding: a bug pattern,
the code that exhibited it, the reviewer's comment, and a fix summary.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class RealCatchExample(BaseModel):
    """A single curated code-review example used for few-shot retrieval."""

    id: str
    bug_pattern: str
    code_snippet: str
    review_comment: str
    fix_summary: str

    @field_validator("id", "bug_pattern", "code_snippet", "review_comment", "fix_summary")
    @classmethod
    def must_be_non_empty(cls, value: str, info: object) -> str:  # noqa: ANN001
        """Reject empty or whitespace-only strings."""
        if not value or not value.strip():
            raise ValueError("Field must be a non-empty string")
        return value


def validate_record(raw: dict[str, object]) -> RealCatchExample:
    """Parse and validate a raw dict into a RealCatchExample.

    Raises ``pydantic.ValidationError`` on malformed input.
    """
    return RealCatchExample.model_validate(raw)
