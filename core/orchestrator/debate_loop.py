"""Main debate loop — run_debate and _run_debate_inner.

In production this is called by worker.py (a queue consumer), not run
directly as a script. ``run_debate`` must be safe to call concurrently
across many (repo, ticket) pairs — each gets its own sandbox, its own
agent instances, and its own DB session.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from google.adk.runners import InMemoryRunner

from core import diagnostics
from core.agents import build_patcher, build_reviewer, close_agent_toolsets
from core.config import ModelConfig, settings
from core.gate import run_full_gate, sandbox_copy
from core.language import detect_language
from core.observability import CostTracker, get_logger, metrics
from core.orchestrator.agent_turn import _ask
from core.orchestrator.counterexample import (
    _check_reviewer_wrote_test,
    _validate_reviewer_counterexample,
)
from core.orchestrator.models import DebateResult, ReviewerVerdict, RoundLog
from core.orchestrator.persistence import (
    _persist_round,
    _persist_session_end,
    _persist_session_start,
    _persist_with_timeout,
)
from core.orchestrator.verdict import _extract_code, _parse_verdict
from core.path_safety import validate_repo_ref

logger = get_logger(__name__)


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
        # verdict = ReviewerVerdict.INCONCLUSIVE
        # result.needs_human_review = True

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
            patcher_key_index = next_key_index  # type: ignore
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
            # verdict = ReviewerVerdict.INCONCLUSIVE
            # result.needs_human_review = True

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
