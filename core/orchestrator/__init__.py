"""
orchestrator — the debate loop mechanics.

In production this is called by worker.py (a queue consumer), not run
directly as a script. `run_debate` must be safe to call concurrently
across many (repo, ticket) pairs — each gets its own sandbox, its own
agent instances, and its own DB session.

Hardening (GAP 5, 6, 7 fixes):
- Retry with exponential backoff on transient LLM API errors (max 3)
- Circuit breaker to fail fast during sustained outages
- Silent code-extraction failure detection + logging
- Reviewer prose-without-test detection + logging
- Per-round persistence so in-flight debates survive crashes
- All print() replaced with structured logging via observability.py

Retrieval (GAP 8, GAP 14):
- Behavioral retrieval (retrieval.py) and repository-context retrieval
  (repo_context.py) both run fresh every round, since the code under
  review changes each round. They are two distinct sources rendered
  into two distinct prompt slots — see agents.py.

Multi-key pooling (GAP 15):
- Both agents are built with a model bound to one key from
  core.llm_client's KeyPool instead of a single shared key. On a
  rate-limit error, _ask() marks the exhausted key cooling-down and
  rotates to a fresh key rather than backing off on the same one — see
  _ask()'s docstring and llm_client.py's module docstring for exactly
  what does and doesn't rotate (the Reviewer rotates every round; the
  Patcher rotates within a debate on a 429, but starts each debate on
  one key drawn from the pool).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export the full old public API so that every existing import of the form
#   from core.orchestrator import X
# continues to resolve without changes anywhere else in the codebase.
# ---------------------------------------------------------------------------

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)

# --- Models ---
from core.orchestrator.models import (  # noqa: E402, F401
    DebateResult,
    ReviewerVerdict,
    RoundLog,
)

# --- Circuit Breaker ---
from core.orchestrator.circuit_breaker import CircuitBreaker  # noqa: E402, F401

# Global circuit breaker instance. Threshold and cooldown are explicit settings
# so operators can tune them from observed provider reliability instead of
# relying on hidden constants.
_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    cooldown_seconds=settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
)

# --- Verdict / code extraction ---
from core.orchestrator.verdict import (  # noqa: E402, F401
    CODE_BLOCK_RE,
    VERDICT_RE,
    _extract_code,
    _parse_verdict,
)

# --- Agent turn ---
from core.orchestrator.agent_turn import _ask  # noqa: E402, F401

# --- Counterexample detection ---
from core.orchestrator.counterexample import (  # noqa: E402, F401
    _check_reviewer_wrote_test,
    _validate_reviewer_counterexample,
)

# --- Persistence ---
from core.orchestrator.persistence import (  # noqa: E402, F401
    _PERSIST_TIMEOUT_SECONDS,
    _persist_round,
    _persist_session_end,
    _persist_session_start,
    _persist_with_timeout,
)

# --- Debate loop ---
from core.orchestrator.debate_loop import (  # noqa: E402, F401
    _run_debate_inner,
    run_debate,
)

# --- CLI ---
from core.orchestrator.cli import print_debate_summary  # noqa: E402, F401


# ---------------------------------------------------------------------------
# Direct script execution (for quick local testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    from storage.db import run_migrations

    run_migrations()

    ticket = (
        "average_price() should return the average unit price of the given "
        "items (0.0 for an empty list). apply_bulk_discount() should give a "
        "10% discount when total quantity across items is >= 50, and must "
        "not mutate the caller's input list/objects — return a new list."
    )
    demo_repo = str(Path(__file__).parent.parent / "demo_repo")
    outcome = asyncio.run(run_debate(demo_repo, "inventory.py", ticket))
    print_debate_summary(outcome)
