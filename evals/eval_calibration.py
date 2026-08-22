"""Regression tests for the read-only calibration report."""

from __future__ import annotations

from core.calibration import build_calibration_report, parse_prometheus_snapshot


def test_prometheus_parser_ignores_metadata_and_malformed_lines():
    samples = parse_prometheus_snapshot(
        """# HELP acr_reviewer_verdicts_total Reviewer verdict distribution
# TYPE acr_reviewer_verdicts_total counter
acr_reviewer_verdicts_total{verdict=\"PASS\"} 4
acr_reviewer_verdicts_total{verdict=\"INCONCLUSIVE\"} 1
not-a-metric
"""
    )

    assert samples[("acr_reviewer_verdicts_total", (("verdict", "PASS"),))] == 4.0
    assert samples[("acr_reviewer_verdicts_total", (("verdict", "INCONCLUSIVE"),))] == 1.0
    assert len(samples) == 2


def test_report_marks_small_samples_insufficient_without_tuning():
    samples = parse_prometheus_snapshot(
        """acr_reviewer_verdicts_total{verdict=\"ISSUE_FOUND\"} 2
acr_reviewer_counterexamples_confirmed_total 1
acr_reviewer_evidence_rejected_total 1
acr_debates_max_rounds_total 1
acr_repo_context_signals_total{signal=\"call_graph\"} 2
"""
    )

    report = build_calibration_report(samples, minimum_sample_size=3)

    assert report["sample_size"] == 2
    assert report["sufficient_sample"] is False
    assert report["evidence"]["rejection_rate"] == 0.5
    assert "Collect more" in report["recommendation"]


def test_report_handles_no_evidence_or_verdict_samples():
    report = build_calibration_report({}, minimum_sample_size=1)

    assert report["sample_size"] == 0
    assert report["inconclusive_rate"] is None
    assert report["evidence"]["rejection_rate"] is None
    assert report["recommendation"]


def test_report_reaches_sufficient_sample_without_mutating_input():
    samples = parse_prometheus_snapshot(
        """acr_reviewer_verdicts_total{verdict=\"PASS\"} 30
acr_reviewer_counterexamples_confirmed_total 8
acr_reviewer_evidence_rejected_total 2
"""
    )
    before = dict(samples)

    report = build_calibration_report(samples)

    assert report["sufficient_sample"] is True
    assert report["verdicts"] == {"PASS": 30}
    assert report["evidence"]["rejection_rate"] == 0.2
    assert samples == before
