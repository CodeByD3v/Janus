"""Session and round persistence helpers.

Real, unresolved-until-now finding from live end-to-end testing (see
ROADMAP.md §2): _persist_session_end was observed to not complete within
the full worker process after a real (failing) LLM call sequence,
despite completing correctly and quickly in isolation. The three
_persist_* functions below are synchronous, blocking DB calls, invoked
directly (unawaited, not offloaded) from inside async functions
(run_debate / _run_debate_inner) — a genuine issue independent of that
mystery: a blocking call inside an async function blocks the ENTIRE
event loop for its duration, which is bad practice regardless of whether
it ever actually hangs. This wrapper does two things at once:
  1. Runs the blocking call in a thread (asyncio.to_thread) so it never
     blocks the event loop even when it's fast.
  2. Applies a hard timeout, so if a persist call ever genuinely hangs
     (rather than raising quickly), that hang becomes a loud, logged
     asyncio.TimeoutError instead of a silent stall that leaves a
     session's true final state ambiguous — exactly what made the
     original finding hard to diagnose.
This does not, by itself, explain WHY a hang might occur — it bounds
the damage and makes the failure mode observable if it recurs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from core.observability import get_logger
from core.orchestrator.models import RoundLog
from storage.db import get_session
from storage.models import DebateSession, Round

logger = get_logger(__name__)

_PERSIST_TIMEOUT_SECONDS = 5.0


async def _persist_with_timeout(
    fn,
    *args,
    timeout: float = _PERSIST_TIMEOUT_SECONDS,
    **kwargs,
) -> bool:
    """Run a synchronous persistence function in a thread, with a timeout.

    Returns True on success. On timeout or any exception, logs clearly
    and returns False rather than raising — a failed/slow persistence
    call must not crash the whole debate; the caller already has its own
    fallback behavior (e.g. run_debate returning a DebateResult even if
    its final DB write didn't land).
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs), timeout=timeout
        )
        return True
    except TimeoutError:
        logger.error(

            "persist_call_timed_out",
            function=getattr(fn, "__name__", repr(fn)),
            timeout_seconds=timeout,
        )
        return False
    except Exception:
        logger.error(
            "persist_call_failed",
            function=getattr(fn, "__name__", repr(fn)),
            exc_info=True,
        )
        return False


def _persist_session_start(
    debate_id: str,
    repo_dir: str,
    target_file: str,
    ticket: str,
    tenant_id: str | None = None,
) -> None:
    """Create or update the DebateSession row to reflect a debate starting.

    This is an UPSERT, not an unconditional insert — a real, verified
    bug, found only by actually running the full stack end-to-end (API
    and worker as separate processes against a real database, not
    mocked): in the real system flow, api/app.py's create_debate()
    already creates this row with status='queued' when the request
    comes in, and worker.py's claim_queued_session() has already UPDATEd
    it to status='running' by the time run_debate() (and this function)
    are even called. The previous unconditional INSERT collided with
    that already-existing row on debate_sessions.id's UNIQUE constraint
    every single time a debate ran through the real API+worker path —
    meaning the documented, intended way of using this system was
    completely non-functional. No unit test caught this: eval_reviewer.py
    calls run_debate() directly with no debate_id, so it always takes the
    fresh-row path and never exercises the collision.

    Still creates a fresh row when none exists (e.g. run_debate() called
    directly, as eval_reviewer.py and eval_gate.py-style direct calls do)
    — both call shapes are real and supported.
    """
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=debate_id).first()
        if session is not None:
            session.status = "running"  # type: ignore[assignment]
            session.repo_ref = repo_dir  # type: ignore[assignment]
            session.target_file = target_file  # type: ignore[assignment]
            session.ticket = ticket  # type: ignore[assignment]
            if tenant_id is not None:
                session.tenant_id = tenant_id  # type: ignore[assignment]
            session.updated_at = datetime.now(UTC)  # type: ignore[assignment]
        else:
            session = DebateSession(
                id=debate_id,
                repo_ref=repo_dir,
                target_file=target_file,
                ticket=ticket,
                status="running",
                tenant_id=tenant_id,
            )
            db.add(session)
    logger.info("debate_session_persisted", debate_id=debate_id, status="running")


def _persist_round(
    debate_id: str,
    round_log: RoundLog,
) -> None:
    """Persist a single round's data immediately after it completes."""
    with get_session() as db:
        db_round = Round(
            session_id=debate_id,
            round_num=round_log.round_num,
            patch_text=round_log.patch_text,
            reviewer_text=round_log.reviewer_text,
            gate_result_json=json.dumps(round_log.gate_result),
            retrieved_example_ids_json=json.dumps(round_log.retrieved_example_ids),
            repo_context_signals_json=json.dumps(round_log.repo_context_signals),
            stop_reason=round_log.stop_reason,
            code_extraction_failed=round_log.code_extraction_failed,
            reviewer_skipped_counterexample=round_log.reviewer_skipped_counterexample,
            reviewer_verdict=round_log.reviewer_verdict,
        )
        db.add(db_round)
    logger.info(
        "round_persisted",
        debate_id=debate_id,
        round_num=round_log.round_num,
        stop_reason=round_log.stop_reason,
    )


def _persist_session_end(
    debate_id: str,
    merged: bool,
    final_gate: dict[str, Any],
    cost: dict[str, Any] | None,
    sandbox_path: str | None,
    error_message: str | None = None,
    reviewer_verdict: str | None = None,
    needs_human_review: bool = False,
) -> None:
    """Update the DebateSession with final results."""
    status = "merged" if merged else "rejected"
    if error_message:
        status = "error"
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=debate_id).first()
        if session:
            session.status = status  # type: ignore[assignment]
            session.merged = merged  # type: ignore[assignment]
            session.final_gate_json = json.dumps(final_gate)  # type: ignore[assignment]
            session.cost_json = json.dumps(cost) if cost else None  # type: ignore[assignment]
            session.sandbox_path = sandbox_path  # type: ignore[assignment]
            session.error_message = error_message  # type: ignore[assignment]
            session.reviewer_verdict = reviewer_verdict  # type: ignore[assignment]
            session.needs_human_review = needs_human_review  # type: ignore[assignment]
            session.updated_at = datetime.now(UTC)  # type: ignore[assignment]
    logger.info(
        "debate_session_completed",
        debate_id=debate_id,
        status=status,
        merged=merged,
        reviewer_verdict=reviewer_verdict,
        needs_human_review=needs_human_review,
    )
