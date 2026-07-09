"""
orchestrator.py — the debate loop mechanics.

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

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from core.agents import build_patcher, build_reviewer
from core.config import settings
from core.gate import run_full_gate, sandbox_copy
from core.llm_client import get_key_pool, is_rate_limit_error
from core.observability import CostTracker, LLMCallStats, get_logger, metrics
from core.path_safety import validate_repo_ref
from storage.db import get_session
from storage.models import DebateSession, Round

logger = get_logger(__name__)

CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Simple circuit breaker for LLM API calls.

    States:
    - closed: requests flow normally
    - open: requests fail fast (after N consecutive failures)
    - half_open: allow one probe request after cooldown

    This prevents holding worker capacity on doomed retries during a
    sustained outage.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._state = "closed"
        self._consecutive_failures = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> str:
        if self._state == "open":
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.cooldown_seconds:
                self._state = "half_open"
                logger.info(
                    "circuit_breaker_half_open",
                    elapsed=elapsed,
                    cooldown=self.cooldown_seconds,
                )
                metrics.circuit_breaker_state = "half_open"
        return self._state

    def record_success(self) -> None:
        if self._state != "closed":
            logger.info("circuit_breaker_closed", previous_state=self._state)
        self._state = "closed"
        self._consecutive_failures = 0
        metrics.circuit_breaker_state = "closed"

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        self._last_failure_time = time.monotonic()
        if self._consecutive_failures >= self.failure_threshold:
            if self._state != "open":
                logger.warning(
                    "circuit_breaker_open",
                    consecutive_failures=self._consecutive_failures,
                    threshold=self.failure_threshold,
                )
                metrics.circuit_breaker_opens.inc()
            self._state = "open"
            metrics.circuit_breaker_state = "open"

    def allow_request(self) -> bool:
        state = self.state
        return state in ("closed", "half_open")


# Global circuit breaker instance
_circuit_breaker = CircuitBreaker()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RoundLog:
    round_num: int
    patch_text: str
    reviewer_text: str
    gate_result: dict[str, Any]
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


# ---------------------------------------------------------------------------
# LLM call with retry + circuit breaker
# ---------------------------------------------------------------------------


async def _ask(
    runner: InMemoryRunner,
    session_id: str,
    user_id: str,
    text: str,
    cost_tracker: CostTracker | None = None,
    max_retries: int = 3,
    key_index: int | None = None,
    rebuild_on_rate_limit: Callable[[], Awaitable[tuple[InMemoryRunner, str, int]]] | None = None,
) -> tuple[str, InMemoryRunner, str, int | None]:
    """Send a message to an agent and collect its response.

    Includes:
    - Retry with exponential backoff (max_retries attempts)
    - Circuit breaker check before each attempt
    - Cost tracking for token/dollar aggregation
    - Key rotation on rate-limit errors (GAP 15): if `key_index` and
      `rebuild_on_rate_limit` are provided and a rate-limit error is
      detected (see llm_client.is_rate_limit_error), the exhausted key
      is marked cooling-down in the shared pool and a fresh
      (runner, session_id, key_index) is drawn before the next attempt,
      instead of backing off and retrying the same rate-limited key.
      This is safe because every prompt in this system is self-contained
      (ticket + current code are always resent in full) — rebuilding the
      underlying agent/session mid-debate loses no state the model needs.

    Returns (response_text, runner, session_id, key_index). The last
    three may differ from what was passed in if a rotation happened —
    callers MUST use the returned values for any subsequent call using
    the same logical agent (e.g. the Patcher across rounds).
    """
    if not _circuit_breaker.allow_request():
        raise RuntimeError(
            "Circuit breaker is OPEN — LLM API has had too many consecutive "
            "failures. Failing fast to avoid wasting resources."
        )

    message = genai_types.Content(role="user", parts=[genai_types.Part(text=text)])
    last_exception: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            start_time = time.monotonic()
            final_text = ""
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if getattr(part, "text", None):
                            final_text += part.text

            duration = time.monotonic() - start_time
            _circuit_breaker.record_success()

            if cost_tracker:
                # Approximate token counts from text length (rough heuristic)
                # Real token counts would come from the API response metadata
                input_tokens = len(text) // 4
                output_tokens = len(final_text) // 4
                cost_tracker.record_call(
                    LLMCallStats(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        estimated_cost_usd=0.0,  # Would be calculated from model pricing
                        duration_seconds=duration,
                        key_index=key_index,
                    )
                )

            return final_text, runner, session_id, key_index

        except Exception as e:
            last_exception = e
            _circuit_breaker.record_failure()
            metrics.llm_retries.inc()

            rotated = False
            if key_index is not None and is_rate_limit_error(e):
                get_key_pool().mark_rate_limited(key_index)
                if rebuild_on_rate_limit is not None and attempt < max_retries:
                    runner, session_id, key_index = await rebuild_on_rate_limit()
                    rotated = True

            logger.warning(
                "llm_call_retry",
                attempt=attempt,
                max_retries=max_retries,
                error=str(e),
                error_type=type(e).__name__,
                rotated_key=rotated,
                key_index=key_index,
            )
            if attempt < max_retries and not rotated:
                # Only back off if we're retrying the SAME key — a fresh
                # key from rotation has its own independent quota, so
                # there's no reason to wait before trying it.
                backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
                await asyncio.sleep(backoff)
            if not _circuit_breaker.allow_request():
                break

    raise RuntimeError(
        f"LLM call failed after {max_retries} attempts. Last error: {last_exception}"
    )


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------


def _extract_code(text: str, fallback: str) -> tuple[str, bool]:
    """Extract a Python code block from the agent's response.

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


# ---------------------------------------------------------------------------
# Reviewer counterexample detection
# ---------------------------------------------------------------------------


def _check_reviewer_wrote_test(
    sandbox: Path, pre_existing_tests: set[str], reviewer_text: str
) -> bool:
    """Check if the Reviewer actually wrote a counterexample test file.

    Returns True if the Reviewer gave a non-empty critique but wrote no
    new test file — i.e. it skipped the counterexample requirement.
    """
    if "no further issues found" in reviewer_text.lower():
        return False  # Reviewer is satisfied, no test expected

    # Check for new test files
    tests_dir = sandbox / "tests"
    if tests_dir.exists():
        current_tests = {f.name for f in tests_dir.iterdir() if f.is_file()}
        new_tests = current_tests - pre_existing_tests
        if new_tests:
            return False  # Reviewer wrote a test — good

    # Reviewer gave a critique but no test
    logger.warning(
        "reviewer_skipped_counterexample",
        reviewer_text_length=len(reviewer_text),
        detail="Reviewer gave a critique but did not write_candidate_test",
    )
    metrics.reviewer_skipped_counterexample.inc()
    return True


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _persist_session_start(
    debate_id: str,
    repo_dir: str,
    target_file: str,
    ticket: str,
    tenant_id: str | None = None,
) -> None:
    """Create the initial DebateSession row."""
    with get_session() as db:
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
            session.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
    logger.info(
        "debate_session_completed",
        debate_id=debate_id,
        status=status,
        merged=merged,
    )


# ---------------------------------------------------------------------------
# Main debate loop
# ---------------------------------------------------------------------------


async def run_debate(
    repo_dir: str,
    target_file: str,
    ticket: str,
    debate_id: str | None = None,
    tenant_id: str | None = None,
) -> DebateResult:
    """Run a complete adversarial code review debate.

    This is the core loop:
    1. Patcher proposes a fix
    2. Reviewer critiques with executable counterexamples
    3. Patcher responds to valid critiques
    4. Repeat until Reviewer is satisfied or round cap hit
    5. Deterministic gate makes the final merge/reject decision

    Safe to call concurrently — each debate gets its own sandbox,
    agent instances, and DB records.
    """

    debate_id = debate_id or str(uuid.uuid4())
    cost_tracker = CostTracker()

    metrics.debates_started.inc()
    logger.info(
        "debate_started",
        debate_id=debate_id,
        repo_dir=repo_dir,
        target_file=target_file,
    )

    # Defense-in-depth: api/schemas.py's field_validator already rejects
    # an out-of-allowlist repo_ref at request time, but this call must not
    # be the ONLY thing standing between an arbitrary repo_dir and
    # shutil.copytree() below — a future caller of run_debate() that
    # doesn't go through the API (a script, a different entrypoint) would
    # otherwise have no protection at all. Same check, re-applied here.
    try:
        validate_repo_ref(repo_dir)
    except ValueError as e:
        error_msg = f"repo_ref rejected: {e}"
        logger.error("debate_failed_repo_ref_validation", debate_id=debate_id, error=error_msg)
        _persist_session_start(debate_id, repo_dir, target_file, ticket, tenant_id)
        _persist_session_end(debate_id, False, {}, cost_tracker.to_dict(), None, error_msg)
        return DebateResult(merged=False, sandbox_path=None)

    # Persist session start
    _persist_session_start(debate_id, repo_dir, target_file, ticket, tenant_id)


    sandbox = sandbox_copy(repo_dir)
    try:
        return await _run_debate_inner(
            repo_dir, target_file, ticket, debate_id, tenant_id, sandbox, cost_tracker
        )
    finally:
        import shutil
        shutil.rmtree(sandbox, ignore_errors=True)

async def _run_debate_inner(
    repo_dir: str,
    target_file: str,
    ticket: str,
    debate_id: str,
    tenant_id: str | None,
    sandbox: Path,
    cost_tracker: CostTracker,
) -> DebateResult:
    # Lazy import to avoid circular dependency at module load time
    from core.repo_context import format_repo_context_for_prompt, retrieve_repo_context
    from core.retrieval import format_examples_for_prompt, retrieve_examples

    sandbox_resolved = sandbox.resolve()
    target_path = (sandbox_resolved / target_file).resolve()

    if not target_path.is_relative_to(sandbox_resolved):
        error_msg = f"Path traversal denied: {target_file} is outside the sandbox"
        logger.error("debate_failed_path_traversal", debate_id=debate_id, error=error_msg)
        _persist_session_end(debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    try:
        current_code = target_path.read_text()
    except Exception as e:
        error_msg = f"Failed to read target file: {e}"
        logger.error("debate_failed_read_target", debate_id=debate_id, error=error_msg)
        _persist_session_end(debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    patcher_agent, patcher_key_index = build_patcher()
    patcher_runner = InMemoryRunner(agent=patcher_agent, app_name=settings.APP_NAME)

    user_id = "service_account"
    patcher_session = str(uuid.uuid4())
    await patcher_runner.session_service.create_session(
        app_name=settings.APP_NAME, user_id=user_id, session_id=patcher_session
    )

    async def _rebuild_patcher() -> tuple[InMemoryRunner, str, int]:
        """Draw a fresh key from the pool and rebuild the Patcher agent,
        runner, and session. Safe mid-debate because every prompt sent to
        the Patcher already carries the full ticket + current code — no
        state is lost by starting a fresh session bound to a new key."""
        agent, idx = build_patcher()
        r = InMemoryRunner(agent=agent, app_name=settings.APP_NAME)
        sid = str(uuid.uuid4())
        await r.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=sid
        )
        return r, sid, idx

    result = DebateResult(merged=False, sandbox_path=str(sandbox))

    # Initial patch
    patch_prompt = (
        f"Ticket:\n{ticket}\n\n"
        f"Current contents of {target_file}:\n```python\n{current_code}\n```\n\n"
        f"Propose your patch as a full replacement file."
    )

    try:
        patch_text, patcher_runner, patcher_session, patcher_key_index = await _ask(
            patcher_runner,
            patcher_session,
            user_id,
            patch_prompt,
            cost_tracker=cost_tracker,
            key_index=patcher_key_index,
            rebuild_on_rate_limit=_rebuild_patcher,
        )
    except RuntimeError as e:
        logger.error("debate_failed_initial_patch", debate_id=debate_id, error=str(e))
        _persist_session_end(debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), str(e))
        return result

    current_code, extraction_failed = _extract_code(patch_text, current_code)
    target_path.write_text(current_code)

    # Snapshot pre-existing test files for counterexample detection
    tests_dir = sandbox / "tests"
    pre_existing_tests: set[str] = set()
    if tests_dir.exists():
        pre_existing_tests = {f.name for f in tests_dir.iterdir() if f.is_file()}

    for round_num in range(1, settings.MAX_ROUNDS + 1):
        metrics.rounds_total.inc()
        logger.info("round_started", debate_id=debate_id, round_num=round_num)

        # Retrieve examples for this round's code
        try:
            examples = retrieve_examples(current_code, top_k=3)
        except Exception as e:
            logger.warning(
                "retrieval_failed",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            examples = []

        # Repo-context retrieval (GAP 14): a separate, structural source —
        # re-read from the live sandbox every round so it always reflects
        # the current patch, not a stale snapshot from round 1.
        try:
            repo_context = retrieve_repo_context(str(sandbox), target_file, current_code)
        except Exception as e:
            logger.warning(
                "repo_context_retrieval_failed",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            repo_context = {}

        reviewer_agent, reviewer_key_index = build_reviewer(
            format_examples_for_prompt(examples),
            format_repo_context_for_prompt(repo_context),
        )
        reviewer_runner = InMemoryRunner(agent=reviewer_agent, app_name=settings.APP_NAME)
        reviewer_session = str(uuid.uuid4())
        await reviewer_runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=reviewer_session
        )

        async def _rebuild_reviewer() -> tuple[InMemoryRunner, str, int]:
            """Draw a fresh key and rebuild the Reviewer for this same
            round, keeping this round's retrieved examples/repo context.
            The Reviewer is rebuilt fresh every round anyway, so this is
            genuinely lossless — nothing about this round's session has
            accumulated yet at the point a rotation would happen."""
            agent, idx = build_reviewer(
                format_examples_for_prompt(examples),
                format_repo_context_for_prompt(repo_context),
            )
            r = InMemoryRunner(agent=agent, app_name=settings.APP_NAME)
            sid = str(uuid.uuid4())
            await r.session_service.create_session(
                app_name=settings.APP_NAME, user_id=user_id, session_id=sid
            )
            return r, sid, idx

        review_prompt = (
            f"Ticket:\n{ticket}\n\n"
            f"Patcher's current version of {target_file} "
            f"(sandbox at {sandbox}):\n```python\n{current_code}\n```\n\n"
            f"The repo root for your tools is: {sandbox}\n"
            f"Review this patch. If you find a real issue, write an "
            f"executable counterexample test and run it to confirm it "
            f"fails, then report the failure. If nothing clears the bar, "
            f"say 'No further issues found.'"
        )

        try:
            reviewer_text, reviewer_runner, reviewer_session, reviewer_key_index = await _ask(
                reviewer_runner,
                reviewer_session,
                user_id,
                review_prompt,
                cost_tracker=cost_tracker,
                key_index=reviewer_key_index,
                rebuild_on_rate_limit=_rebuild_reviewer,
            )
        except RuntimeError as e:
            logger.error(
                "debate_failed_reviewer",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            break

        gate_result = run_full_gate(str(sandbox))

        # Track gate check pass/fail by type
        for check in gate_result.get("checks", []):
            outcome = f"{check['check']}_{'pass' if check['passed'] else 'fail'}"
            metrics.gate_checks.inc(outcome)

        # Detect if Reviewer gave a critique without a counterexample test
        skipped_counterexample = _check_reviewer_wrote_test(
            sandbox, pre_existing_tests, reviewer_text
        )

        stop_reason = None
        if "no further issues found" in reviewer_text.lower():
            stop_reason = "reviewer_satisfied"
        elif round_num == settings.MAX_ROUNDS:
            stop_reason = "max_rounds_reached"

        round_log = RoundLog(
            round_num=round_num,
            patch_text=patch_text,
            reviewer_text=reviewer_text,
            gate_result=gate_result,
            retrieved_example_ids=[ex.get("id", "") for ex in examples],
            repo_context_signals={
                "callers": repo_context.get("call_graph", {}).get("callers", []),
                "prior_fix_shas": [f.get("sha", "") for f in repo_context.get("prior_fixes", [])],
                "test_convention_files": len(repo_context.get("test_conventions", [])),
            },
            stop_reason=stop_reason,
            code_extraction_failed=extraction_failed,
            reviewer_skipped_counterexample=skipped_counterexample,
        )
        result.rounds.append(round_log)

        # Persist round immediately (survives crashes)
        _persist_round(debate_id, round_log)

        if stop_reason:
            logger.info(
                "debate_loop_stop",
                debate_id=debate_id,
                round_num=round_num,
                reason=stop_reason,
            )
            break

        # Update pre_existing_tests for next round
        if tests_dir.exists():
            pre_existing_tests = {f.name for f in tests_dir.iterdir() if f.is_file()}

        fix_prompt = (
            f"Reviewer's critique:\n{reviewer_text}\n\n"
            f"Current contents of {target_file}:\n```python\n{current_code}\n```\n\n"
            f"Fix the issue if it's real, or explain briefly why you're "
            f"pushing back (only once per critique). Propose your patch as "
            f"a full replacement file."
        )

        try:
            patch_text, patcher_runner, patcher_session, patcher_key_index = await _ask(
                patcher_runner,
                patcher_session,
                user_id,
                fix_prompt,
                cost_tracker=cost_tracker,
                key_index=patcher_key_index,
                rebuild_on_rate_limit=_rebuild_patcher,
            )
        except RuntimeError as e:
            logger.error(
                "debate_failed_patcher_fix",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            break

        current_code, extraction_failed = _extract_code(patch_text, current_code)
        target_path.write_text(current_code)

    # Final gate
    final_gate = run_full_gate(str(sandbox))
    result.final_gate = final_gate
    result.merged = final_gate["passed"]
    result.cost = cost_tracker.to_dict()

    # Update metrics
    metrics.debates_completed.inc()
    metrics.rounds_per_debate.observe(len(result.rounds))
    if result.merged:
        metrics.debates_merged.inc()
    else:
        metrics.debates_rejected.inc()

    # Persist final state
    _persist_session_end(
        debate_id,
        result.merged,
        final_gate,
        cost_tracker.to_dict(),
        str(sandbox),
    )

    logger.info(
        "debate_completed",
        debate_id=debate_id,
        merged=result.merged,
        rounds=len(result.rounds),
        cost=cost_tracker.to_dict(),
    )

    return result


def print_debate_summary(result: DebateResult) -> None:
    """Print a human-readable summary of a debate result (for CLI use)."""
    print(f"Sandbox: {result.sandbox_path}")
    for r in result.rounds:
        print(f"\n--- Round {r.round_num} ---")
        print("Reviewer:", r.reviewer_text[:400])
        print("Gate at this round:", "PASS" if r.gate_result["passed"] else "FAIL")
        if r.code_extraction_failed:
            print("  ⚠ Code extraction failed this round")
        if r.reviewer_skipped_counterexample:
            print("  ⚠ Reviewer gave critique without a counterexample test")
        if r.stop_reason:
            print("Stop reason:", r.stop_reason)
    if result.final_gate:
        print("\n=== FINAL GATE ===")
        for c in result.final_gate["checks"]:
            print(f"  [{'OK' if c['passed'] else 'FAIL'}] {c['check']}")
        print("MERGED" if result.merged else "REJECTED — did not pass the gate")
    if result.cost:
        print(f"\nCost: {result.cost}")


if __name__ == "__main__":
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
