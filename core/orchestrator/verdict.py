"""Verdict parsing and code extraction from agent responses."""

from __future__ import annotations

import re

from core.observability import get_logger, metrics
from core.orchestrator.models import ReviewerVerdict

logger = get_logger(__name__)

CODE_BLOCK_RE = re.compile(r"```(?:\w+)?\s*\n(.*?)```", re.DOTALL)

# Matches the structured verdict line emitted by the Reviewer per the
# prompt contract in agents.py's REVIEWER_INSTRUCTION_TEMPLATE.
VERDICT_RE = re.compile(
    r"VERDICT:\s*(PASS|ISSUE_FOUND|INCONCLUSIVE)(?![A-Z_])",
    re.IGNORECASE,
)


def _parse_verdict(reviewer_text: str) -> ReviewerVerdict:
    """Extract the Reviewer's structured verdict from its output.

    Falls back to legacy heuristic ("no further issues found" → PASS)
    for backward compatibility with Reviewer outputs that predate the
    verdict-line prompt addition.  If neither matches, defaults to
    ISSUE_FOUND (conservative: assume something was flagged).
    """
    # Models occasionally revise a verdict in the same response. The final
    # explicit verdict is the one that reflects the model's settled answer.
    matches = list(VERDICT_RE.finditer(reviewer_text))
    if matches:
        raw = matches[-1].group(1).upper()
        try:
            return ReviewerVerdict(raw)
        except ValueError:
            pass

    # Legacy fallback
    if "no further issues found" in reviewer_text.lower():
        return ReviewerVerdict.PASS
    return ReviewerVerdict.ISSUE_FOUND


def _extract_code(text: str, fallback: str) -> tuple[str, bool]:
    """Extract a fenced code block from the agent's response.

    Returns (code, extraction_failed). If no code block is found,
    returns the fallback and True so the caller can log the failure.
    """
    match = CODE_BLOCK_RE.search(text)
    if match:
        return match.group(1), False
    logger.warning(
        "code_extraction_failed",
        response_length=len(text),
        detail="Patcher response contained no fenced code block",
    )
    metrics.code_extraction_failed.inc()
    return fallback, True
