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
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from core import diagnostics
from core.agents import build_patcher, build_reviewer, close_agent_toolsets
from core.config import ModelConfig, settings
from core.gate import run_candidate_test, run_full_gate, sandbox_copy
from core.language import detect_language
from core.llm_client import get_key_pool, is_rate_limit_error
from core.observability import CostTracker, LLMCallStats, get_logger, metrics
from core.path_safety import validate_repo_ref
from storage.db import get_session
from storage.models import DebateSession, Round

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
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        transitioned = False
        elapsed = 0.0
        with self._lock:
            if self._state == "open":
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.cooldown_seconds:
                    self._state = "half_open"
                    self._probe_in_flight = False
                    transitioned = True
            state = self._state

        if transitioned:
            logger.info(
                "circuit_breaker_half_open",
                elapsed=elapsed,
                cooldown=self.cooldown_seconds,
            )
            metrics.circuit_breaker_state = "half_open"
        return state

    def record_success(self) -> None:
        with self._lock:
            previous_state = self._state
            self._state = "closed"
            self._consecutive_failures = 0
            self._probe_in_flight = False
        if previous_state != "closed":
            logger.info("circuit_breaker_closed", previous_state=previous_state)
        metrics.circuit_breaker_state = "closed"

    def record_failure(self) -> None:
        opened = False
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.monotonic()
            self._probe_in_flight = False
            if self._consecutive_failures >= self.failure_threshold:
                opened = self._state != "open"
                self._state = "open"
        if opened:
            logger.warning(
                "circuit_breaker_open",
                consecutive_failures=self._consecutive_failures,
                threshold=self.failure_threshold,
            )
            metrics.circuit_breaker_opens.inc()
        # Do not consult the property here: with a zero cooldown it can
        # transition immediately to half_open and misreport the just-opened
        # breaker. The state update belongs to this transition itself.
        if opened:
            metrics.circuit_breaker_state = "open"

    def allow_request(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state != "half_open":
            return False
        with self._lock:
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True


# Global circuit breaker instance. Threshold and cooldown are explicit settings
# so operators can tune them from observed provider reliability instead of
# relying on hidden constants.
_circuit_breaker = CircuitBreaker(
    failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    cooldown_seconds=settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


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

            async def _collect_response(
                current_runner: InMemoryRunner = runner,
                current_session_id: str = session_id,
                current_message: genai_types.Content = message,
            ) -> str:
                final_text = ""
                async for event in current_runner.run_async(
                    user_id=user_id,
                    session_id=current_session_id,
                    new_message=current_message,
                ):
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            part_text = getattr(part, "text", None)
                            if isinstance(part_text, str) and part_text:
                                final_text += part_text
                return final_text

            final_text = await asyncio.wait_for(
                _collect_response(),
                timeout=settings.LLM_CALL_TIMEOUT_SECONDS,
            )
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
    # Use the structured verdict to determine if a test is expected.
    # PASS and INCONCLUSIVE don't require a counterexample test.
    verdict = _parse_verdict(reviewer_text)
    if verdict in (ReviewerVerdict.PASS, ReviewerVerdict.INCONCLUSIVE):
        return False  # Reviewer is satisfied or inconclusive, no test expected

    # Check for new test files using recursive paths. Nested test suites and
    # duplicate basenames must not be confused with pre-existing top-level files.
    tests_dir = sandbox / "tests"
    if tests_dir.exists():
        current_tests = {
            f.relative_to(tests_dir).as_posix()
            for f in tests_dir.rglob("*")
            if f.is_file()
        }
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


def _validate_reviewer_counterexample(
    sandbox: Path,
    pre_existing_tests: set[str],
    reviewer_text: str,
) -> tuple[bool, str]:
    """Require an ISSUE_FOUND critique to produce an executed failing test.

    A newly created file is not sufficient evidence: it may be skipped by
    pytest, pass, fail during collection, or target the wrong path. The
    Reviewer contract is satisfied only when ``run_candidate_test`` executes
    the new file and pytest reports at least one failed test.
    """
    if _parse_verdict(reviewer_text) != ReviewerVerdict.ISSUE_FOUND:
        return True, "not_required"

    tests_dir = sandbox / "tests"
    if not tests_dir.exists():
        metrics.reviewer_evidence_rejected.inc()
        return False, "tests_directory_missing"

    new_files = sorted(
        path
        for path in tests_dir.rglob("*")
        if path.is_file()
        and path.relative_to(tests_dir).as_posix() not in pre_existing_tests
    )
    if not new_files:
        metrics.reviewer_evidence_rejected.inc()
        return False, "counterexample_not_written"

    for path in new_files:
        filename = path.relative_to(tests_dir).as_posix()
        result = run_candidate_test(str(sandbox), filename)
        detail = str(result.get("detail", ""))
        detail_lower = detail.lower()
        executed_failure = (
            not bool(result.get("passed"))
            and re.search(r"\b\d+\s+failed\b", detail_lower) is not None
            and "no tests ran" not in detail_lower
            and "file not found" not in detail_lower
            and "error collecting" not in detail_lower
        )
        if executed_failure:
            metrics.reviewer_counterexamples_confirmed.inc()
            return True, filename

    metrics.reviewer_evidence_rejected.inc()
    return False, "counterexample_did_not_execute_and_fail"


# ---------------------------------------------------------------------------
# Persistence helpers

# ---------------------------------------------------------------------------

# Real, unresolved-until-now finding from live end-to-end testing (see
# ROADMAP.md §2): _persist_session_end was observed to not complete within
# the full worker process after a real (failing) LLM call sequence,
# despite completing correctly and quickly in isolation. The three
# _persist_* functions below are synchronous, blocking DB calls, invoked
# directly (unawaited, not offloaded) from inside async functions
# (run_debate / _run_debate_inner) — a genuine issue independent of that
# mystery: a blocking call inside an async function blocks the ENTIRE
# event loop for its duration, which is bad practice regardless of whether
# it ever actually hangs. This wrapper does two things at once:
#   1. Runs the blocking call in a thread (asyncio.to_thread) so it never
#      blocks the event loop even when it's fast.
#   2. Applies a hard timeout, so if a persist call ever genuinely hangs
#      (rather than raising quickly), that hang becomes a loud, logged
#      asyncio.TimeoutError instead of a silent stall that leaves a
#      session's true final state ambiguous — exactly what made the
#      original finding hard to diagnose.
# This does not, by itself, explain WHY a hang might occur — it bounds
# the damage and makes the failure mode observable if it recurs.
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


# ---------------------------------------------------------------------------
# Main debate loop
# ---------------------------------------------------------------------------


async def run_debate(
    repo_dir: str,
    target_file: str,
    ticket: str,
    debate_id: str | None = None,
    tenant_id: str | None = None,
    model_config: ModelConfig | None = None,
) -> DebateResult:
    """Run a complete adversarial code review debate (Reviewer-first).

    The Reviewer examines the existing code first and returns a verdict:
    - PASS: code is fine → run final gate → merge if it passes
    - INCONCLUSIVE: can't determine → flag for human review, no merge
    - ISSUE_FOUND: concrete bug found → Patcher fixes → iterate

    Only ISSUE_FOUND invokes the Patcher. Good PRs cost exactly one LLM
    call (the initial review), preventing the Patcher from "fixing"
    things that aren't broken.

    model_config: Optional BYOK configuration. If None, uses the
    server-default Google Gemini model. The debate engine treats this
    as opaque configuration (Hard Rule 12).

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
        await _persist_with_timeout(_persist_session_start, debate_id, repo_dir, target_file, ticket, tenant_id)
        await _persist_with_timeout(_persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), None, error_msg)
        return DebateResult(merged=False, sandbox_path=None)

    # Persist session start
    await _persist_with_timeout(_persist_session_start, debate_id, repo_dir, target_file, ticket, tenant_id)


    sandbox: Path | None = None
    baseline_sandbox: Path | None = None
    try:
        sandbox = await asyncio.to_thread(sandbox_copy, repo_dir)
        baseline_sandbox = await asyncio.to_thread(sandbox_copy, repo_dir)
        return await _run_debate_inner(
            repo_dir, target_file, ticket, debate_id, tenant_id,
            sandbox, baseline_sandbox, cost_tracker, model_config,
        )
    finally:
        import shutil
        if baseline_sandbox is not None:
            shutil.rmtree(baseline_sandbox, ignore_errors=True)
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)


async def _run_debate_inner(
    repo_dir: str,
    target_file: str,
    ticket: str,
    debate_id: str,
    tenant_id: str | None,
    sandbox: Path,
    baseline_sandbox: Path,
    cost_tracker: CostTracker,
    model_config: ModelConfig | None = None,
) -> DebateResult:
    """Reviewer-first debate loop (Janus 2.0).

    Flow:
    1. Reviewer examines the EXISTING code (no Patcher yet).
    2. Parse VERDICT: PASS → gate check → done.
       INCONCLUSIVE → flag for human review → done.
       ISSUE_FOUND → proceed to step 3.
    3. Build Patcher, fix the specific issues Reviewer found.
    4. Run validation, Reviewer re-reviews.
    5. Repeat steps 3-4 until PASS, INCONCLUSIVE, or MAX_ROUNDS.
    6. Final gate makes the merge/reject decision.
    """
    # Lazy import to avoid circular dependency at module load time
    from core.repo_context import format_repo_context_for_prompt, retrieve_repo_context
    from core.retrieval import format_examples_for_prompt, retrieve_examples

    sandbox_resolved = sandbox.resolve()
    target_path = (sandbox_resolved / target_file).resolve()

    if not target_path.is_relative_to(sandbox_resolved):
        error_msg = f"Path traversal denied: {target_file} is outside the sandbox"
        logger.error("debate_failed_path_traversal", debate_id=debate_id, error=error_msg)
        await _persist_with_timeout(_persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    try:
        current_code = target_path.read_text()
    except Exception as e:
        error_msg = f"Failed to read target file: {e}"
        logger.error("debate_failed_read_target", debate_id=debate_id, error=error_msg)
        await _persist_with_timeout(_persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), error_msg)
        return DebateResult(merged=False, sandbox_path=str(sandbox))

    # Detect language from target file for language-agnostic prompts
    language = detect_language(target_file)

    user_id = "service_account"
    result = DebateResult(merged=False, sandbox_path=str(sandbox))

    # Snapshot pre-existing test files for counterexample detection
    tests_dir = sandbox / "tests"
    pre_existing_tests: set[str] = set()
    if tests_dir.exists():
        pre_existing_tests = {f.relative_to(tests_dir).as_posix() for f in tests_dir.rglob("*") if f.is_file()}

    # -- Helper: build and call the Reviewer for a given round -----------

    async def _run_reviewer(
        round_num: int,
        code: str,
        is_initial: bool,
    ) -> tuple[str, ReviewerVerdict, list, dict]:
        """Build a fresh Reviewer, call it, parse its verdict.

        Returns (reviewer_text, verdict, examples, repo_context_dict).
        """
        # Retrieve examples for this round's code
        try:
            examples = retrieve_examples(code, top_k=3)
        except Exception as e:
            logger.warning(
                "retrieval_failed",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            examples = []

        # Repo-context retrieval — re-read from the live sandbox every
        # round so it always reflects the current patch.
        try:
            repo_ctx = retrieve_repo_context(
                str(sandbox),
                target_file,
                code,
                history_repo_dir=repo_dir,
            )
            call_graph = repo_ctx.get("call_graph", {})
            metrics.repo_context_signals.inc(
                "callers_present" if call_graph.get("callers") else "callers_empty"
            )
            metrics.repo_context_signals.inc(
                "prior_fixes_present" if repo_ctx.get("prior_fixes") else "prior_fixes_empty"
            )
            metrics.repo_context_signals.inc(
                "tests_present" if repo_ctx.get("test_conventions") else "tests_empty"
            )

        except Exception as e:
            logger.warning(
                "repo_context_retrieval_failed",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            repo_ctx = {}

        reviewer_agent, reviewer_key_index = build_reviewer(
            format_examples_for_prompt(examples),
            format_repo_context_for_prompt(repo_ctx),
            language=language,
            model_config=model_config,
        )
        reviewer_runner = InMemoryRunner(
            agent=reviewer_agent, app_name=settings.APP_NAME
        )
        reviewer_session = str(uuid.uuid4())
        await reviewer_runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=reviewer_session
        )

        async def _rebuild_reviewer() -> tuple[InMemoryRunner, str, int]:
            """Rebuild the reviewer and close its old MCP subprocess first."""
            nonlocal reviewer_agent
            await close_agent_toolsets(reviewer_agent)
            reviewer_agent, idx = build_reviewer(
                format_examples_for_prompt(examples),
                format_repo_context_for_prompt(repo_ctx),
                language=language,
                model_config=model_config,
            )
            runner = InMemoryRunner(
                agent=reviewer_agent, app_name=settings.APP_NAME
            )
            session_id = str(uuid.uuid4())
            await runner.session_service.create_session(
                app_name=settings.APP_NAME, user_id=user_id, session_id=session_id
            )
            return runner, session_id, idx

        context_label = (
            f"Current contents of {target_file}"
            if is_initial
            else f"Patcher's current version of {target_file}"
        )

        review_prompt = (
            f"Ticket:\n{ticket}\n\n"
            f"{context_label} "
            f"(sandbox at {sandbox}):\n```{language}\n{code}\n```\n\n"
            f"The repo root for your tools is: {sandbox}\n"
            f"Review this code. If you find a real issue, write an "
            f"executable counterexample test and run it to confirm it "
            f"fails, then report the failure. If nothing clears the bar, "
            f"say 'No further issues found.' End with your VERDICT line."
        )

        try:
            text, _, _, _ = await _ask(
                reviewer_runner,
                reviewer_session,
                user_id,
                review_prompt,
                cost_tracker=cost_tracker,
                key_index=reviewer_key_index,
                rebuild_on_rate_limit=_rebuild_reviewer,
            )
        finally:
            await close_agent_toolsets(reviewer_agent)

        verdict = _parse_verdict(text)
        return text, verdict, examples, repo_ctx

    # ===================================================================
    # PHASE 1: Initial Review (Reviewer goes first on existing code)
    # ===================================================================

    diagnostics.trace("before_initial_review", debate_id=debate_id)
    metrics.rounds_total.inc()
    logger.info("round_started", debate_id=debate_id, round_num=0, phase="initial_review")

    try:
        reviewer_text, verdict, examples, repo_context = await _run_reviewer(
            round_num=0, code=current_code, is_initial=True
        )
        diagnostics.trace("after_initial_review", debate_id=debate_id, verdict=verdict.value)
    except RuntimeError as e:
        logger.error("debate_failed_initial_review", debate_id=debate_id, error=str(e))
        diagnostics.trace("initial_review_raised_runtimeerror", debate_id=debate_id, error=str(e)[:200])
        persisted = await _persist_with_timeout(
            _persist_session_end, debate_id, False, {}, cost_tracker.to_dict(), str(sandbox), str(e)
        )
        if not persisted:
            logger.error(
                "debate_final_state_not_persisted",
                debate_id=debate_id,
                detail="Initial review failed and the failure state could not be "
                       "persisted — see persist_call_timed_out/persist_call_failed "
                       "above. sweep_zombie_sessions will eventually recover this "
                       "session if it's left stuck in 'running'.",
            )
        return result

    # Detect if Reviewer gave a critique without a counterexample test
    skipped_counterexample = _check_reviewer_wrote_test(
        sandbox, pre_existing_tests, reviewer_text
    )

    counterexample_ok, evidence_reason = _validate_reviewer_counterexample(
        sandbox, pre_existing_tests, reviewer_text
    )
    if verdict == ReviewerVerdict.ISSUE_FOUND and not counterexample_ok:
        logger.warning(
            "reviewer_issue_without_executable_evidence",
            debate_id=debate_id,
            evidence_reason=evidence_reason,
        )
        skipped_counterexample = True
        verdict = ReviewerVerdict.INCONCLUSIVE
        result.needs_human_review = True

    metrics.reviewer_verdicts.inc(verdict.value)

    # Record the initial review as round 0 (Reviewer-only, no patch)

    initial_round = RoundLog(
        round_num=0,
        patch_text="",  # No Patcher ran yet
        reviewer_text=reviewer_text,
        gate_result={},  # No gate ran yet
        reviewer_verdict=verdict.value,
        retrieved_example_ids=[ex.get("id", "") for ex in examples],
        repo_context_signals={
            "callers": repo_context.get("call_graph", {}).get("callers", []),
            "prior_fix_shas": [f.get("sha", "") for f in repo_context.get("prior_fixes", [])],
            "test_convention_files": len(repo_context.get("test_conventions", [])),
        },
        stop_reason=None,
        code_extraction_failed=False,
        reviewer_skipped_counterexample=skipped_counterexample,
    )
    result.rounds.append(initial_round)
    result.reviewer_verdict = verdict.value
    await _persist_with_timeout(_persist_round, debate_id, initial_round)

    logger.info(
        "initial_review_complete",
        debate_id=debate_id,
        verdict=verdict.value,
        skipped_counterexample=skipped_counterexample,
    )

    # ---------------------------------------------------------------
    # PASS: Code is fine → run final gate → done
    # ---------------------------------------------------------------
    if verdict == ReviewerVerdict.PASS:
        logger.info("reviewer_passed", debate_id=debate_id)
        final_gate = await asyncio.to_thread(
            run_full_gate,
            str(sandbox),
            target_file,
            baseline_repo_dir=str(baseline_sandbox),
        )

        result.final_gate = final_gate
        result.merged = final_gate["passed"]
        result.cost = cost_tracker.to_dict()
        result.reviewer_verdict = ReviewerVerdict.PASS.value

        metrics.debates_completed.inc()
        metrics.rounds_per_debate.observe(len(result.rounds))
        if result.merged:
            metrics.debates_merged.inc()
        else:
            metrics.debates_rejected.inc()

        await _persist_with_timeout(
            _persist_session_end,
            debate_id,
            result.merged,
            final_gate,
            cost_tracker.to_dict(),
            str(sandbox),
            reviewer_verdict=ReviewerVerdict.PASS.value,
        )
        logger.info(
            "debate_completed",
            debate_id=debate_id,
            merged=result.merged,
            rounds=len(result.rounds),
            verdict="PASS",
        )
        return result

    # ---------------------------------------------------------------
    # INCONCLUSIVE: Flag for human review → done (no Patcher)
    # ---------------------------------------------------------------
    if verdict == ReviewerVerdict.INCONCLUSIVE:
        logger.info("reviewer_inconclusive", debate_id=debate_id)
        result.needs_human_review = True
        result.reviewer_verdict = ReviewerVerdict.INCONCLUSIVE.value
        result.cost = cost_tracker.to_dict()

        metrics.debates_completed.inc()
        metrics.rounds_per_debate.observe(len(result.rounds))
        metrics.debates_rejected.inc()

        await _persist_with_timeout(
            _persist_session_end,
            debate_id,
            False,  # Never auto-merge on INCONCLUSIVE
            {},     # No final gate run
            cost_tracker.to_dict(),
            str(sandbox),
            reviewer_verdict=ReviewerVerdict.INCONCLUSIVE.value,
            needs_human_review=True,
        )
        logger.info(
            "debate_completed",
            debate_id=debate_id,
            merged=False,
            rounds=len(result.rounds),
            verdict="INCONCLUSIVE",
            needs_human_review=True,
        )
        return result

    # ===================================================================
    # PHASE 2: ISSUE_FOUND — Patcher fixes, iterate
    # ===================================================================

    logger.info("reviewer_issue_found", debate_id=debate_id)

    # Build Patcher only now — not before the Reviewer has found an issue
    diagnostics.trace("before_build_patcher", debate_id=debate_id)
    patcher_agent, patcher_key_index = build_patcher(language=language, model_config=model_config)
    patcher_runner = InMemoryRunner(agent=patcher_agent, app_name=settings.APP_NAME)
    diagnostics.trace("after_build_patcher", debate_id=debate_id)

    patcher_session = str(uuid.uuid4())
    await patcher_runner.session_service.create_session(
        app_name=settings.APP_NAME, user_id=user_id, session_id=patcher_session
    )

    async def _rebuild_patcher() -> tuple[InMemoryRunner, str, int]:
        """Rebuild the Patcher and close its old MCP subprocess first."""
        nonlocal patcher_agent
        await close_agent_toolsets(patcher_agent)
        patcher_agent, idx = build_patcher(
            language=language, model_config=model_config
        )
        runner = InMemoryRunner(agent=patcher_agent, app_name=settings.APP_NAME)
        session_id = str(uuid.uuid4())
        await runner.session_service.create_session(
            app_name=settings.APP_NAME, user_id=user_id, session_id=session_id
        )
        return runner, session_id, idx

    last_reviewer_text = reviewer_text
    extraction_failed = False

    for round_num in range(1, settings.MAX_ROUNDS + 1):
        metrics.rounds_total.inc()
        logger.info("round_started", debate_id=debate_id, round_num=round_num, phase="patcher_fix")

        # -- Patcher fixes the specific issues the Reviewer found -------

        fix_prompt = (
            f"The Reviewer has identified the following issues with concrete "
            f"failing tests. Fix ONLY these specific issues. Do not make "
            f"unrelated changes.\n\n"
            f"Reviewer critique:\n{last_reviewer_text}\n\n"
            f"Current contents of {target_file}:\n```{language}\n{current_code}\n```\n\n"
            f"Ticket:\n{ticket}\n\n"
            f"Propose your patch as a full replacement file in a fenced "
            f"{language} code block."
        )

        try:
            patch_text, patcher_runner, patcher_session, next_key_index = await _ask(
                patcher_runner,
                patcher_session,
                user_id,
                fix_prompt,
                cost_tracker=cost_tracker,
                key_index=patcher_key_index,
                rebuild_on_rate_limit=_rebuild_patcher,
            )
            patcher_key_index = next_key_index
        except RuntimeError as e:
            logger.error(
                "debate_failed_patcher_fix",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            await close_agent_toolsets(patcher_agent)
            break

        current_code, extraction_failed = _extract_code(patch_text, current_code)

        target_path.write_text(current_code)

        # -- Run validation on the patched code -------------------------

        gate_result = await asyncio.to_thread(
            run_full_gate,
            str(sandbox),
            target_file,
            baseline_repo_dir=str(baseline_sandbox),
        )

        # Track gate check pass/fail by type
        for check in gate_result.get("checks", []):
            outcome = f"{check['check']}_{'pass' if check['passed'] else 'fail'}"
            metrics.gate_checks.inc(outcome)

        # -- Reviewer re-reviews the patched code -----------------------

        # Update pre_existing_tests for counterexample detection
        if tests_dir.exists():
            pre_existing_tests = {f.relative_to(tests_dir).as_posix() for f in tests_dir.rglob("*") if f.is_file()}

        try:
            reviewer_text, verdict, examples, repo_context = await _run_reviewer(
                round_num=round_num, code=current_code, is_initial=False
            )
        except RuntimeError as e:
            logger.error(
                "debate_failed_reviewer",
                debate_id=debate_id,
                round_num=round_num,
                error=str(e),
            )
            # Record partial round (Patcher ran but Reviewer failed)
            round_log = RoundLog(
                round_num=round_num,
                patch_text=patch_text,
                reviewer_text="",
                gate_result=gate_result,
                reviewer_verdict=ReviewerVerdict.ISSUE_FOUND.value,
                stop_reason="reviewer_error",
                code_extraction_failed=extraction_failed,
            )
            result.rounds.append(round_log)
            await _persist_with_timeout(_persist_round, debate_id, round_log)
            await close_agent_toolsets(patcher_agent)
            break

        skipped_counterexample = _check_reviewer_wrote_test(
            sandbox, pre_existing_tests, reviewer_text
        )
        counterexample_ok, evidence_reason = _validate_reviewer_counterexample(
            sandbox, pre_existing_tests, reviewer_text
        )
        if verdict == ReviewerVerdict.ISSUE_FOUND and not counterexample_ok:
            logger.warning(
                "reviewer_issue_without_executable_evidence",
                debate_id=debate_id,
                round_num=round_num,
                evidence_reason=evidence_reason,
            )
            skipped_counterexample = True
            verdict = ReviewerVerdict.INCONCLUSIVE
            result.needs_human_review = True

        metrics.reviewer_verdicts.inc(verdict.value)

        # Determine stop reason

        stop_reason = None
        if verdict == ReviewerVerdict.PASS:
            stop_reason = "reviewer_satisfied"
        elif verdict == ReviewerVerdict.INCONCLUSIVE:
            stop_reason = "reviewer_inconclusive"
            result.needs_human_review = True
        elif round_num == settings.MAX_ROUNDS:
            stop_reason = "max_rounds_reached"
            metrics.debates_max_rounds.inc()

        round_log = RoundLog(

            round_num=round_num,
            patch_text=patch_text,
            reviewer_text=reviewer_text,
            gate_result=gate_result,
            reviewer_verdict=verdict.value,
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
        result.reviewer_verdict = verdict.value

        # Persist round immediately (survives crashes)
        await _persist_with_timeout(_persist_round, debate_id, round_log)

        if stop_reason:
            logger.info(
                "debate_loop_stop",
                debate_id=debate_id,
                round_num=round_num,
                reason=stop_reason,
                verdict=verdict.value,
            )
            break

        last_reviewer_text = reviewer_text

    await close_agent_toolsets(patcher_agent)

    # ===================================================================

    # PHASE 3: Final gate — sole merge authority
    # ===================================================================

    final_gate = await asyncio.to_thread(
        run_full_gate,
        str(sandbox),
        target_file,
        baseline_repo_dir=str(baseline_sandbox),
    )

    result.final_gate = final_gate

    result.merged = final_gate["passed"] and not result.needs_human_review
    result.cost = cost_tracker.to_dict()

    # Update metrics
    metrics.debates_completed.inc()
    metrics.rounds_per_debate.observe(len(result.rounds))
    if result.merged:
        metrics.debates_merged.inc()
    else:
        metrics.debates_rejected.inc()

    # Persist final state
    persisted = await _persist_with_timeout(
        _persist_session_end,
        debate_id,
        result.merged,
        final_gate,
        cost_tracker.to_dict(),
        str(sandbox),
        reviewer_verdict=result.reviewer_verdict,
        needs_human_review=result.needs_human_review,
    )
    if not persisted:
        logger.error(
            "debate_final_state_not_persisted",
            debate_id=debate_id,
            merged=result.merged,
            detail="A successfully completed debate's final state could not be "
                   "persisted — see persist_call_timed_out/persist_call_failed "
                   "above. sweep_zombie_sessions will eventually recover this "
                   "session if it's left stuck in 'running'.",
        )

    logger.info(
        "debate_completed",
        debate_id=debate_id,
        merged=result.merged,
        rounds=len(result.rounds),
        verdict=result.reviewer_verdict,
        needs_human_review=result.needs_human_review,
        cost=cost_tracker.to_dict(),
    )

    return result


def print_debate_summary(result: DebateResult) -> None:
    """Print a human-readable summary of a debate result (for CLI use)."""
    print(f"Sandbox: {result.sandbox_path}")
    print(f"Verdict: {result.reviewer_verdict}")
    if result.needs_human_review:
        print("⚠ Flagged for human review (INCONCLUSIVE)")
    for r in result.rounds:
        if r.round_num == 0:
            print("\n--- Initial Review (Round 0) ---")
            print(f"Verdict: {r.reviewer_verdict}")
        else:
            print(f"\n--- Round {r.round_num} ---")
            print(f"Verdict: {r.reviewer_verdict}")
        print("Reviewer:", r.reviewer_text[:400])
        if r.gate_result and r.gate_result.get("passed") is not None:
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
