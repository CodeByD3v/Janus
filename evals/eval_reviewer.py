"""
evals/eval_reviewer.py — integration test for the full debate loop.

Requires GOOGLE_API_KEY to be set. Skipped otherwise.
Runs a complete adversarial code review debate on the demo_repo and
asserts the patch merges within MAX_ROUNDS.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytestmark = pytest.mark.integration

_HAS_API_KEY = bool(os.environ.get("GOOGLE_API_KEY"))


@pytest.mark.skipif(not _HAS_API_KEY, reason="GOOGLE_API_KEY not set")
@pytest.mark.timeout(300)
class TestFullDebate:
    """Integration test: run the complete Patcher ↔ Reviewer debate loop."""

    def test_full_debate_merges(self) -> None:
        """Run a full debate on demo_repo with the standard ticket.

        Asserts:
        - The debate completes without crashing
        - At least 1 round occurred
        - The final gate passed (merged=True)
        """
        from storage.db import run_migrations
        from core.orchestrator import run_debate

        run_migrations()

        ticket = (
            "average_price() should return the average unit price of the given "
            "items (0.0 for an empty list). apply_bulk_discount() should give a "
            "10% discount when total quantity across items is >= 50, and must "
            "not mutate the caller's input list/objects — return a new list."
        )
        demo_repo = str(Path(__file__).resolve().parent.parent / "demo_repo")

        result = asyncio.run(run_debate(demo_repo, "inventory.py", ticket))

        # At least 1 round occurred
        assert len(result.rounds) >= 1, (
            f"Expected at least 1 round, got {len(result.rounds)}"
        )

        # Final gate exists
        assert result.final_gate is not None, "No final gate result"

        # Patch merged
        assert result.merged is True, (
            f"Expected merge, got rejected. "
            f"Gate: {result.final_gate}"
        )

        assert result.final_gate["passed"] is True
