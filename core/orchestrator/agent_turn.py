"""LLM agent turn execution with retry, circuit breaker, and key rotation."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable

from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from core.llm_client import extract_retry_delay, get_key_pool, is_rate_limit_error
from core.observability import CostTracker, LLMCallStats, get_logger, metrics

logger = get_logger(__name__)


def _get_package():
    """Return the core.orchestrator package module.

    _ask reads _circuit_breaker and settings from this module so that
    monkeypatching ``core.orchestrator._circuit_breaker`` or
    ``core.orchestrator.settings`` in tests affects _ask at call time.
    """
    return sys.modules["core.orchestrator"]


async def _ask(
    runner: InMemoryRunner,
    session_id: str,
    user_id: str,
    text: str,
    cost_tracker: CostTracker | None = None,
    max_retries: int = 5,
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
    pkg = _get_package()
    _circuit_breaker = pkg._circuit_breaker
    settings = pkg.settings

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
            rate_limited = is_rate_limit_error(e)
            if key_index is not None and rate_limited:
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

            if attempt < max_retries:
                if rate_limited:
                    # Respect the API's recommended retry delay. On free-tier
                    # keys (5 req/min), this is typically 15-25s. Without this,
                    # the retry logic burns through all attempts in ~5s and the
                    # debate fails unnecessarily.
                    api_delay = extract_retry_delay(e)
                    if api_delay is not None:
                        backoff = min(api_delay + 1.0, 60.0)  # cap at 60s
                    else:
                        # Fallback: longer backoff for rate limits
                        backoff = min(10 * (2 ** (attempt - 1)), 60.0)  # 10s, 20s, 40s
                    logger.info(
                        "llm_rate_limit_backoff",
                        backoff_seconds=backoff,
                        api_recommended=api_delay,
                        attempt=attempt,
                    )
                elif not rotated:
                    # Non-rate-limit error, use standard exponential backoff
                    backoff = 2 ** (attempt - 1)  # 1s, 2s, 4s
                else:
                    backoff = 0  # Fresh key from rotation, no wait needed
                if backoff > 0:
                    await asyncio.sleep(backoff)
            if not _circuit_breaker.allow_request():
                break

    raise RuntimeError(
        f"LLM call failed after {max_retries} attempts. Last error: {last_exception}"
    )
