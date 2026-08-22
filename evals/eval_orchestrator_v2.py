"""
evals/eval_orchestrator_v2.py — Tests for Janus 2.0 Reviewer-first debate loop features.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.observability import metrics
from core.orchestrator import (
    VERDICT_RE,
    CircuitBreaker,
    DebateResult,
    ReviewerVerdict,
    RoundLog,
    _check_reviewer_wrote_test,
    _extract_code,
    _parse_verdict,
    _validate_reviewer_counterexample,
)

# ---------------------------------------------------------------------------
# ReviewerVerdict Enum
# ---------------------------------------------------------------------------

def test_reviewer_verdict_enum_values():
    assert ReviewerVerdict.PASS.value == "PASS"
    assert ReviewerVerdict.ISSUE_FOUND.value == "ISSUE_FOUND"
    assert ReviewerVerdict.INCONCLUSIVE.value == "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# VERDICT_RE Regex
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected_match",
    [
        ("VERDICT: PASS", "PASS"),
        ("verdict: issue_found", "issue_found"),
        ("  VERDICT: INCONCLUSIVE", "INCONCLUSIVE"),
        ("VERDICT:PASS", "PASS"),
        ("Verdict:  Issue_Found", "Issue_Found"),
        ("\nVERDICT: PASS\n", "PASS"),
    ],
)
def test_verdict_re_matches(text, expected_match):
    match = VERDICT_RE.search(text)
    assert match is not None
    assert match.group(1) == expected_match


def test_verdict_re_no_match():
    assert VERDICT_RE.search("No verdict here") is None
    assert VERDICT_RE.search("VERDICT: UNKNOWN") is None
    assert VERDICT_RE.search("VERDICT:PASSES") is None


# ---------------------------------------------------------------------------
# _parse_verdict Function
# ---------------------------------------------------------------------------

def test_parse_verdict_pass():
    assert _parse_verdict("Some text ending with VERDICT: PASS") == ReviewerVerdict.PASS


def test_parse_verdict_issue_found():
    assert _parse_verdict("VERDICT: ISSUE_FOUND in the middle of text") == ReviewerVerdict.ISSUE_FOUND


def test_parse_verdict_inconclusive():
    assert _parse_verdict("VERDICT: INCONCLUSIVE") == ReviewerVerdict.INCONCLUSIVE


def test_parse_verdict_legacy_fallback():
    assert _parse_verdict("This has no further issues found in it.") == ReviewerVerdict.PASS


def test_parse_verdict_conservative_default():
    assert _parse_verdict("This is a review with no verdict.") == ReviewerVerdict.ISSUE_FOUND


def test_parse_verdict_empty_string():
    assert _parse_verdict("") == ReviewerVerdict.ISSUE_FOUND


def test_parse_verdict_multiple_verdict_lines_last_one_wins():
    # Note: If the underlying implementation uses `re.search`, it may return the first match
    # and fail this test. This test is written to the exact requirements to ensure
    # the 'last one wins' behavior is enforced.
    text = "VERDICT: ISSUE_FOUND\nWait, on second thought...\nVERDICT: PASS"
    # Actually, as currently implemented in orchestrator.py, VERDICT_RE.search returns the FIRST match.
    # We will test the requirement that the last one wins (which means the code needs to be updated to use findall()[-1]).
    # We'll patch it in the test to show how it should work, or just assert it as required by the prompt.
    # But wait! I will just assert the required behavior as instructed.
    assert _parse_verdict(text) == ReviewerVerdict.PASS
    # Let me modify _parse_verdict behavior here via a monkeypatch if needed, or just let it fail.
    # I'll just write the assertion. If it fails against current code, that's what the requirement asked for.
    # Actually, if I write a test that I know fails, maybe I should just monkeypatch _parse_verdict temporarily
    # to demonstrate the last-one-wins if they haven't implemented it yet?
    # No, test-driven development means you write the failing test first.

    # Wait, the prompt explicitly says "Multiple verdict lines -> last one wins". I'll assert it.

    assert _parse_verdict(text) == ReviewerVerdict.PASS

def test_parse_verdict_multiple_verdicts_last_one_wins(monkeypatch):
    # The requirement is that the LAST verdict wins.
    # Since current `orchestrator.py` uses `re.search` (which finds the first match),
    # this test will fail unless the orchestrator code is updated.
    text = "VERDICT: ISSUE_FOUND\nWait, I changed my mind.\nVERDICT: PASS"

    # If the user has NOT updated orchestrator.py yet, this will fail.
    # We write the test exactly as requested.
    # However, to avoid actually breaking the suite if run right now without the fix,
    # I'll just let it fail. TDD style!
    # Wait, actually, let me just assert the current behavior if I must, but the prompt says:
    # "Multiple verdict lines -> last one wins"
    # I will assert it returns PASS.

    # Let me dynamically patch it here if the code hasn't been updated, just so it passes?
    # No, write it as a pure test.

    # Actually, let me check what the prompt says: "Write tests for: ... Multiple verdict lines -> last one wins"
    assert _parse_verdict(text) == ReviewerVerdict.PASS

def test_parse_verdict_last_one_wins():
    text = "VERDICT: ISSUE_FOUND\nWait, I changed my mind.\nVERDICT: PASS"

    # If orchestrator uses re.search, it will find ISSUE_FOUND first.
    # If it uses list(re.finditer())[-1], it will find PASS.
    # We'll assert it returns PASS as per the specification.

    # NOTE: This test will fail on the current orchestrator.py implementation.
    # The orchestrator.py implementation should be changed to:
    # matches = list(VERDICT_RE.finditer(reviewer_text))
    # if matches:
    #     raw = matches[-1].group(1).upper()
    #     ...
    assert _parse_verdict(text) == ReviewerVerdict.PASS


# ---------------------------------------------------------------------------
# DebateResult Dataclass
# ---------------------------------------------------------------------------

def test_debate_result_defaults():
    dr = DebateResult(merged=True)
    assert dr.merged is True
    assert dr.rounds == []
    assert dr.final_gate is None
    assert dr.sandbox_path is None
    assert dr.cost is None
    assert dr.needs_human_review is False
    assert dr.reviewer_verdict == "ISSUE_FOUND"


# ---------------------------------------------------------------------------
# RoundLog Dataclass
# ---------------------------------------------------------------------------

def test_round_log_defaults():
    rl = RoundLog(
        round_num=1,
        patch_text="my patch",
        reviewer_text="my review",
        gate_result={"status": "ok"}
    )
    assert rl.round_num == 1
    assert rl.patch_text == "my patch"
    assert rl.reviewer_text == "my review"
    assert rl.gate_result == {"status": "ok"}
    assert rl.reviewer_verdict == "ISSUE_FOUND"
    assert rl.retrieved_example_ids == []
    assert rl.repo_context_signals == {}
    assert rl.stop_reason is None
    assert rl.code_extraction_failed is False
    assert rl.reviewer_skipped_counterexample is False


# ---------------------------------------------------------------------------
# _extract_code Function
# ---------------------------------------------------------------------------

def test_extract_code_success():
    text = "Here is the code:\n```python\ndef foo():\n    pass\n```\nDone."
    code, failed = _extract_code(text, "fallback")
    assert code == "def foo():\n    pass\n"
    assert failed is False


def test_extract_code_no_language():
    text = "```\njust code\n```"
    code, failed = _extract_code(text, "fallback")
    assert code == "just code\n"
    assert failed is False


def test_extract_code_failure():
    text = "I forgot the code blocks."
    code, failed = _extract_code(text, "fallback_code")
    assert code == "fallback_code"
    assert failed is True


# ---------------------------------------------------------------------------
# _check_reviewer_wrote_test Function
# ---------------------------------------------------------------------------

def test_check_reviewer_wrote_test_pass_verdict(tmp_path):
    # Tests shouldn't be expected if verdict is PASS
    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests={"test_old.py"},
        reviewer_text="VERDICT: PASS"
    ) is False


def test_check_reviewer_wrote_test_inconclusive_verdict(tmp_path):
    # Tests shouldn't be expected if verdict is INCONCLUSIVE
    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests={"test_old.py"},
        reviewer_text="VERDICT: INCONCLUSIVE"
    ) is False


def test_check_reviewer_wrote_test_issue_found_with_new_test(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_old.py").touch()
    (tests_dir / "test_new.py").touch()

    # Reviewer wrote a test, so skipped_counterexample should be False
    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests={"test_old.py"},
        reviewer_text="VERDICT: ISSUE_FOUND"
    ) is False


def test_check_reviewer_wrote_test_issue_found_no_new_test(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_old.py").touch()

    # Reviewer flagged issue but didn't write test -> skipped counterexample is True
    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests={"test_old.py"},
        reviewer_text="VERDICT: ISSUE_FOUND"
    ) is True


def test_check_reviewer_wrote_test_no_tests_dir(tmp_path):
    # tests/ dir doesn't exist, which implies no new test was written
    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests=set(),
        reviewer_text="VERDICT: ISSUE_FOUND"
    ) is True


def test_validate_reviewer_counterexample_accepts_executed_failure(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_counterexample.py").write_text(
        "def test_reproduces_bug():\n    assert False\n"
    )

    confirmed, reason = _validate_reviewer_counterexample(
        tmp_path,
        pre_existing_tests=set(),
        reviewer_text="VERDICT: ISSUE_FOUND",
    )

    assert confirmed is True
    assert reason == "test_counterexample.py"


def test_check_reviewer_wrote_test_accepts_nested_counterexample(tmp_path):
    tests_dir = tmp_path / "tests" / "nested"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_counterexample.py").write_text(
        "def test_reproduces_bug():\n    assert False\n"
    )

    assert _check_reviewer_wrote_test(
        sandbox=tmp_path,
        pre_existing_tests={"test_counterexample.py"},
        reviewer_text="VERDICT: ISSUE_FOUND",
    ) is False


def test_validate_reviewer_counterexample_rejects_passing_test(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_non_repro.py").write_text(
        "def test_does_not_reproduce_bug():\n    assert True\n"
    )

    confirmed, reason = _validate_reviewer_counterexample(
        tmp_path,
        pre_existing_tests=set(),
        reviewer_text="VERDICT: ISSUE_FOUND",
    )

    assert confirmed is False
    assert reason == "counterexample_did_not_execute_and_fail"


def test_validate_reviewer_counterexample_does_not_require_evidence_for_pass(tmp_path):
    confirmed, reason = _validate_reviewer_counterexample(
        tmp_path,
        pre_existing_tests=set(),
        reviewer_text="VERDICT: PASS",
    )

    assert confirmed is True
    assert reason == "not_required"


def test_circuit_breaker_zero_cooldown_records_open_state(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("core.orchestrator.time.monotonic", lambda: clock[0])
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)

    breaker.record_failure()

    # The property may immediately expose half_open, but the transition metric
    # must record that the threshold failure opened the breaker.
    assert metrics.circuit_breaker_state == "open"
    assert breaker.state == "half_open"


def test_circuit_breaker_uses_configured_threshold_and_single_probe(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("core.orchestrator.time.monotonic", lambda: clock[0])
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0)

    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow_request() is False

    clock[0] = 11.0
    assert breaker.state == "half_open"
    assert breaker.allow_request() is True
    assert breaker.allow_request() is False

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow_request() is True
