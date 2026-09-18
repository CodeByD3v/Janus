"""Circuit breaker for LLM API calls."""

from __future__ import annotations

import threading
import time

from core.observability import get_logger, metrics

logger = get_logger(__name__)


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
