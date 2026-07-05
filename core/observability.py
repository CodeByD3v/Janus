"""
observability.py — structured logging, metrics, and cost tracking.

Provides:
- A single JSON-structured logger used across all modules (replaces print())
- Counters and histograms for operational metrics
- A Prometheus-compatible /metrics endpoint (mounted by the API)
- Per-call token/cost tracking for LLM calls

Usage:
    from observability import get_logger, metrics
    logger = get_logger(__name__)
    logger.info("debate_started", debate_id=debate_id)
    metrics.debates_started.inc()
"""

from __future__ import annotations

import json
import logging
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Structured JSON Logger
# ---------------------------------------------------------------------------

class StructuredFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge extra fields passed via logger.info("msg", extra={...})
        # or via the adapter's process() method
        if hasattr(record, "_extra"):
            log_entry.update(record._extra)  # type: ignore[attr-defined]
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


class StructuredLogger(logging.LoggerAdapter):
    """Logger adapter that accepts keyword arguments as structured fields."""

    def process(
        self, msg: str, kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        extra = kwargs.get("extra", {})
        # Pull out any non-standard kwargs and stuff them into extra._extra
        structured_extra: dict[str, Any] = {}
        keys_to_pop = []
        for key, value in kwargs.items():
            if key not in ("exc_info", "stack_info", "stacklevel", "extra"):
                structured_extra[key] = value
                keys_to_pop.append(key)
        for key in keys_to_pop:
            kwargs.pop(key)
        if structured_extra:
            extra["_extra"] = structured_extra
            kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str) -> StructuredLogger:
    """Create a structured logger for the given module name."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return StructuredLogger(logger, {})


# ---------------------------------------------------------------------------
# Metrics — lightweight counters and histograms
# ---------------------------------------------------------------------------

class Counter:
    """Thread-safe monotonic counter."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._value: float = 0.0
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def prometheus_text(self) -> str:
        return (
            f"# HELP {self.name} {self.help_text}\n"
            f"# TYPE {self.name} counter\n"
            f"{self.name} {self.value}\n"
        )


class LabeledCounter:
    """Thread-safe counter with a single label dimension."""

    def __init__(self, name: str, help_text: str, label_name: str) -> None:
        self.name = name
        self.help_text = help_text
        self.label_name = label_name
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def inc(self, label_value: str, amount: float = 1.0) -> None:
        with self._lock:
            self._values[label_value] = self._values.get(label_value, 0.0) + amount

    def prometheus_text(self) -> str:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            for label, value in sorted(self._values.items()):
                lines.append(f'{self.name}{{{self.label_name}="{label}"}} {value}')
        return "\n".join(lines) + "\n"


class Histogram:
    """Simple histogram tracking sum and count (no bucket boundaries)."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._sum: float = 0.0
        self._count: int = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def total(self) -> float:
        with self._lock:
            return self._sum

    def prometheus_text(self) -> str:
        return (
            f"# HELP {self.name} {self.help_text}\n"
            f"# TYPE {self.name} summary\n"
            f"{self.name}_sum {self.total}\n"
            f"{self.name}_count {self.count}\n"
        )


@dataclass
class MetricsRegistry:
    """Central registry of all application metrics."""

    # Debate lifecycle
    debates_started: Counter = field(
        default_factory=lambda: Counter(
            "acr_debates_started_total", "Total debates started"
        )
    )
    debates_completed: Counter = field(
        default_factory=lambda: Counter(
            "acr_debates_completed_total", "Total debates completed"
        )
    )
    debates_merged: Counter = field(
        default_factory=lambda: Counter(
            "acr_debates_merged_total", "Total debates where patch merged"
        )
    )
    debates_rejected: Counter = field(
        default_factory=lambda: Counter(
            "acr_debates_rejected_total", "Total debates where patch was rejected"
        )
    )

    # Rounds
    rounds_total: Counter = field(
        default_factory=lambda: Counter(
            "acr_rounds_total", "Total debate rounds executed"
        )
    )
    rounds_per_debate: Histogram = field(
        default_factory=lambda: Histogram(
            "acr_rounds_per_debate", "Number of rounds per debate"
        )
    )

    # Gate
    gate_checks: LabeledCounter = field(
        default_factory=lambda: LabeledCounter(
            "acr_gate_checks_total",
            "Gate check results by check type and outcome",
            "check_outcome",
        )
    )

    # Retries / circuit breaker
    llm_retries: Counter = field(
        default_factory=lambda: Counter(
            "acr_llm_retries_total", "Total LLM call retry attempts"
        )
    )
    circuit_breaker_opens: Counter = field(
        default_factory=lambda: Counter(
            "acr_circuit_breaker_opens_total",
            "Times circuit breaker transitioned to open",
        )
    )
    circuit_breaker_state: str = "closed"

    # Reviewer behavior
    reviewer_skipped_counterexample: Counter = field(
        default_factory=lambda: Counter(
            "acr_reviewer_skipped_counterexample_total",
            "Times Reviewer gave prose critique without a test",
        )
    )
    code_extraction_failed: Counter = field(
        default_factory=lambda: Counter(
            "acr_code_extraction_failed_total",
            "Times Patcher response had no extractable code block",
        )
    )

    # Cost tracking
    llm_tokens_input: Counter = field(
        default_factory=lambda: Counter(
            "acr_llm_tokens_input_total", "Total input tokens sent to LLM"
        )
    )
    llm_tokens_output: Counter = field(
        default_factory=lambda: Counter(
            "acr_llm_tokens_output_total", "Total output tokens received from LLM"
        )
    )
    llm_cost_usd: Counter = field(
        default_factory=lambda: Counter(
            "acr_llm_cost_usd_total", "Estimated total LLM cost in USD"
        )
    )
    llm_call_duration: Histogram = field(
        default_factory=lambda: Histogram(
            "acr_llm_call_duration_seconds", "Duration of LLM API calls"
        )
    )

    def prometheus_text(self) -> str:
        """Render all metrics in Prometheus exposition format."""
        parts = []
        for attr_name in sorted(vars(self)):
            attr = getattr(self, attr_name)
            if hasattr(attr, "prometheus_text"):
                parts.append(attr.prometheus_text())
        return "\n".join(parts)


# Global metrics singleton
metrics = MetricsRegistry()


# ---------------------------------------------------------------------------
# Cost tracking helper
# ---------------------------------------------------------------------------

@dataclass
class LLMCallStats:
    """Stats for a single LLM call, for per-debate cost aggregation."""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_seconds: float = 0.0


class CostTracker:
    """Aggregates LLM call costs for a single debate session."""

    def __init__(self) -> None:
        self.calls: list[LLMCallStats] = []
        self._lock = threading.Lock()

    def record_call(self, stats: LLMCallStats) -> None:
        with self._lock:
            self.calls.append(stats)
            metrics.llm_tokens_input.inc(stats.input_tokens)
            metrics.llm_tokens_output.inc(stats.output_tokens)
            metrics.llm_cost_usd.inc(stats.estimated_cost_usd)
            metrics.llm_call_duration.observe(stats.duration_seconds)

    @property
    def total_input_tokens(self) -> int:
        with self._lock:
            return sum(c.input_tokens for c in self.calls)

    @property
    def total_output_tokens(self) -> int:
        with self._lock:
            return sum(c.output_tokens for c in self.calls)

    @property
    def total_cost_usd(self) -> float:
        with self._lock:
            return sum(c.estimated_cost_usd for c in self.calls)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_calls": len(self.calls),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_cost_usd": round(self.total_cost_usd, 6),
            }
