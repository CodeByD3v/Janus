"""
llm_client.py — multi-API-key pooling for LLM calls (GAP 15 / TASK 16).

Without this, every Patcher and Reviewer call across every concurrent
debate, across every tenant, draws from a single Google API key's quota
— that key becomes the throughput ceiling regardless of how many workers
are running. This module lets the service round-robin across several
keys and cool down any key that just hit a rate limit.

HOW KEY BINDING ACTUALLY WORKS (this matters — read before changing it):
ADK's `Gemini` model class resolves its `google.genai.Client` lazily via
a `@cached_property` called `api_client`, which by default falls back to
the `GOOGLE_API_KEY` environment variable if no key is passed explicitly.
Mutating that env var per-call is unreliable once a client has been
cached, and racy across concurrent debates in the same process (worker.py
runs multiple debates concurrently — see GAP 10). ADK's own documented
customization path is to subclass `Gemini` and override `api_client`
directly (see google/adk/models/google_llm.py's class docstring) — that's
what `_KeyedGemini` below does, binding one specific key at construction
time rather than relying on process-global environment state.

WHAT THIS DOES AND DOES NOT ROTATE:
- The Reviewer agent is already rebuilt fresh every round (see
  orchestrator.run_debate), so it gets a freshly-drawn key from the pool
  every round — this is genuine per-round rotation.
- The Patcher agent's LlmAgent + InMemoryRunner + session are built once
  per debate and reused across rounds, because InMemoryRunner owns its
  own in-memory session/conversation state — rebuilding it mid-debate to
  swap keys means starting a fresh session and losing that conversation
  history. So the Patcher gets ONE key drawn from the pool per debate,
  not per round. Different concurrent debates still spread their
  Patchers across the pool, which is the throughput problem this module
  is actually solving; true mid-debate Patcher key rotation on a 429
  would require either accepting a session/history reset or moving
  Patcher conversation state out of InMemoryRunner entirely — noted here
  as a real limitation, not silently glossed over.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from google.adk.models import Gemini
from google.genai import Client

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)


class _KeyedGemini(Gemini):
    """A Gemini model instance bound to one specific API key.

    `bound_api_key` is a real pydantic field (not a private attribute)
    because Gemini/BaseLlm is a pydantic BaseModel — attributes not
    declared as fields cannot be set on an instance after construction.
    Falls back to ADK's normal environment-variable resolution if left
    empty, so this class is safe to use even outside the key pool.
    """

    bound_api_key: str = ""

    @cached_property
    def api_client(self) -> Client:
        base_url, api_version = self._base_url_and_api_version
        kwargs_for_http_options: dict[str, Any] = {
            "headers": self._tracking_headers(),
            "retry_options": self.retry_options,
            "base_url": base_url,
        }
        if api_version:
            kwargs_for_http_options["api_version"] = api_version

        from google.genai import types as genai_types

        kwargs: dict[str, Any] = {
            "http_options": genai_types.HttpOptions(**kwargs_for_http_options),
        }
        if self.model.startswith("projects/"):
            kwargs["enterprise"] = True
        if self.bound_api_key:
            kwargs["api_key"] = self.bound_api_key

        return Client(**kwargs)


@dataclass
class _KeyState:
    key: str
    index: int
    cooldown_until: float = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until


class KeyPool:
    """Round-robins across configured API keys, cooling down any key that
    just hit a rate limit so subsequent picks skip it until the cooldown
    elapses. Never logs or exposes a raw key — only its index.
    """

    def __init__(
        self,
        keys: list[str] | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        raw_keys = keys if keys is not None else settings.google_api_keys()
        if not raw_keys:
            raise RuntimeError(
                "No Google API keys configured. Set GOOGLE_API_KEYS "
                "(comma-separated) or GOOGLE_API_KEY."
            )
        self._keys = [_KeyState(key=k, index=i) for i, k in enumerate(raw_keys)]
        self._cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else settings.GOOGLE_API_KEY_COOLDOWN_SECONDS
        )
        self._cycle = itertools.cycle(range(len(self._keys)))
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    def get_key(self) -> tuple[str, int]:
        """Return (key, index) for the next available key, round-robin,
        skipping any key currently cooling down.

        If every key is cooling down, returns the one closest to
        becoming available rather than raising — a debate should still
        be able to make an attempt and surface the real rate-limit error
        if it's genuinely still too soon, rather than failing before it
        even tries.
        """
        with self._lock:
            n = len(self._keys)
            best_fallback_idx = 0
            for _ in range(n):
                idx = next(self._cycle)
                state = self._keys[idx]
                if state.available:
                    return state.key, state.index
                if state.cooldown_until < self._keys[best_fallback_idx].cooldown_until:
                    best_fallback_idx = idx

            state = self._keys[best_fallback_idx]
            logger.warning("key_pool_all_cooling_down", chosen_index=state.index)
            return state.key, state.index

    def mark_rate_limited(self, index: int) -> None:
        """Cool down the key at `index` for GOOGLE_API_KEY_COOLDOWN_SECONDS,
        so subsequent get_key() calls skip it until it elapses."""
        with self._lock:
            if not (0 <= index < len(self._keys)):
                return
            state = self._keys[index]
            state.cooldown_until = time.monotonic() + self._cooldown_seconds
            logger.warning(
                "key_pool_cooldown_started",
                key_index=index,
                cooldown_seconds=self._cooldown_seconds,
            )

    def key_count(self) -> int:
        return len(self._keys)


_pool: KeyPool | None = None
_pool_lock = threading.Lock()


def get_key_pool() -> KeyPool:
    """Process-wide singleton pool, built lazily from settings on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = KeyPool()
    return _pool


def build_model(model_name: str) -> tuple[_KeyedGemini, int]:
    """Build a Gemini model instance bound to the next available key from
    the pool. Returns (model_instance, key_index) so callers can report
    the index back to mark_rate_limited() on a 429 without ever handling
    the raw key value."""
    pool = get_key_pool()
    key, index = pool.get_key()
    model = _KeyedGemini(model=model_name, bound_api_key=key)
    return model, index


def is_rate_limit_error(exc: Exception) -> bool:
    """Best-effort classification of a rate-limit/quota error from the
    underlying google-genai client, so orchestrator.py's retry loop can
    decide whether to rotate keys (rate limit) or just back off and
    retry the same key (other transient errors)."""
    message = str(exc).lower()
    return (
        "429" in message
        or "rate limit" in message
        or "resource_exhausted" in message
        or "quota" in message
        or "too many requests" in message
    )
