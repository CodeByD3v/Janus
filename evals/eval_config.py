"""Configuration and calibration-control regressions."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Settings
from core.observability import MetricsRegistry


def test_debate_control_defaults_are_safe():
    settings = Settings()

    assert settings.MAX_ROUNDS >= 1
    assert settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD >= 1
    assert settings.CIRCUIT_BREAKER_COOLDOWN_SECONDS >= 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAX_ROUNDS", 0),
        ("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 0),
        ("CIRCUIT_BREAKER_COOLDOWN_SECONDS", -1.0),
    ],
)
def test_unsafe_debate_controls_fail_closed(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_calibration_metrics_are_exported():
    metrics = MetricsRegistry()
    metrics.reviewer_verdicts.inc("ISSUE_FOUND")
    metrics.reviewer_counterexamples_confirmed.inc()
    metrics.repo_context_signals.inc("callers_empty")

    output = metrics.prometheus_text()

    assert 'acr_reviewer_verdicts_total{verdict="ISSUE_FOUND"} 1.0' in output
    assert "acr_reviewer_counterexamples_confirmed_total 1.0" in output
    assert 'acr_repo_context_signals_total{signal="callers_empty"} 1.0' in output
