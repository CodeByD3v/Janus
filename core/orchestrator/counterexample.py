"""Reviewer counterexample detection and validation."""

from __future__ import annotations

import re
from pathlib import Path

from core.gate import run_candidate_test
from core.observability import get_logger, metrics
from core.orchestrator.models import ReviewerVerdict
from core.orchestrator.verdict import _parse_verdict

logger = get_logger(__name__)


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
