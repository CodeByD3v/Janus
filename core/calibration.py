"""Offline calibration summaries for Reviewer and debate telemetry.

This module deliberately does not mutate settings or tune thresholds. It turns
Prometheus exposition snapshots into auditable rates so operators can choose
configuration changes from observed data rather than guesses.
"""

from __future__ import annotations

import re
from typing import Any

_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+"
    r"(?P<value>[-+0-9.eE]+)$"
)
_LABEL_RE = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"\\])*)"')


def parse_prometheus_snapshot(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Parse simple Prometheus samples emitted by :mod:`core.observability`.

    HELP, TYPE, blank, and malformed lines are ignored. The parser is kept
    intentionally narrow so an operator cannot accidentally treat a histogram
    bucket or arbitrary text as a calibration signal.
    """
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        match = _SAMPLE_RE.match(line.strip())
        if match is None:
            continue
        labels_text = match.group("labels") or ""
        labels = tuple(
            sorted((item.group("key"), item.group("value")) for item in _LABEL_RE.finditer(labels_text))
        )
        try:
            samples[(match.group("name"), labels)] = float(match.group("value"))
        except ValueError:
            continue
    return samples


def _counter(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
) -> float:
    return samples.get((name, ()), 0.0)


def _labeled_total(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    name: str,
    label_name: str,
) -> dict[str, float]:
    result: dict[str, float] = {}
    for (sample_name, labels), value in samples.items():
        if sample_name != name:
            continue
        label_value = dict(labels).get(label_name)
        if label_value is not None:
            result[label_value] = value
    return result


def build_calibration_report(
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float],
    *,
    minimum_sample_size: int = 30,
) -> dict[str, Any]:
    """Build a read-only calibration report from a metric snapshot."""
    verdicts = _labeled_total(samples, "acr_reviewer_verdicts_total", "verdict")
    verdict_total = sum(verdicts.values())
    evidence_confirmed = _counter(samples, "acr_reviewer_counterexamples_confirmed_total")
    evidence_rejected = _counter(samples, "acr_reviewer_evidence_rejected_total")
    evidence_total = evidence_confirmed + evidence_rejected
    max_rounds = _counter(samples, "acr_debates_max_rounds_total")
    context_signals = _labeled_total(samples, "acr_repo_context_signals_total", "signal")

    evidence_rejection_rate = evidence_rejected / evidence_total if evidence_total else None
    inconclusive_rate = verdicts.get("INCONCLUSIVE", 0.0) / verdict_total if verdict_total else None
    report: dict[str, Any] = {
        "sample_size": int(verdict_total),
        "minimum_sample_size": minimum_sample_size,
        "sufficient_sample": verdict_total >= minimum_sample_size,
        "verdicts": {key: int(value) for key, value in sorted(verdicts.items())},
        "evidence": {
            "confirmed": int(evidence_confirmed),
            "rejected": int(evidence_rejected),
            "rejection_rate": evidence_rejection_rate,
        },
        "max_round_terminations": int(max_rounds),
        "inconclusive_rate": inconclusive_rate,
        "repository_context_signals": {
            key: int(value) for key, value in sorted(context_signals.items())
        },
        "recommendation": (
            "Collect more production or replay data before changing thresholds."
            if verdict_total < minimum_sample_size
            else "Review these observed rates with an operator before changing thresholds; no automatic tuning was applied."
        ),
    }
    return report


def report_from_prometheus_text(text: str, *, minimum_sample_size: int = 30) -> dict[str, Any]:
    """Parse a Prometheus snapshot and return its calibration report."""
    return build_calibration_report(
        parse_prometheus_snapshot(text), minimum_sample_size=minimum_sample_size
    )


__all__ = [
    "build_calibration_report",
    "parse_prometheus_snapshot",
    "report_from_prometheus_text",
]


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(report_from_prometheus_text(sys.stdin.read()), indent=2, sort_keys=True))
