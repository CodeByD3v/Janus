"""
evals/eval_llm_client.py — multi-API-key pool tests (GAP 15 / TASK 16).

Pure-logic tests, no real API key needed — KeyPool never makes a network
call itself, and _KeyedGemini's api_client is only exercised for
construction, never invoked against the real API here.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_client import (  # noqa: E402
    KeyPool,
    _KeyedGemini,
    build_model,
    get_key_pool,
    is_rate_limit_error,
)

# ---------------------------------------------------------------------------
# KeyPool construction
# ---------------------------------------------------------------------------


def test_pool_requires_at_least_one_key():
    with pytest.raises(RuntimeError):
        KeyPool(keys=[])


def test_pool_len_matches_key_count():
    pool = KeyPool(keys=["a", "b", "c"])
    assert len(pool) == 3
    assert pool.key_count() == 3


# ---------------------------------------------------------------------------
# Round robin
# ---------------------------------------------------------------------------


def test_round_robin_cycles_through_all_keys():
    pool = KeyPool(keys=["a", "b", "c"])
    picks = [pool.get_key()[1] for _ in range(9)]
    assert picks == [0, 1, 2, 0, 1, 2, 0, 1, 2]


def test_get_key_returns_actual_key_value():
    pool = KeyPool(keys=["key-a", "key-b"])
    key, index = pool.get_key()
    assert key in ("key-a", "key-b")
    assert index in (0, 1)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------


def test_cooldown_skips_the_rate_limited_key():
    pool = KeyPool(keys=["a", "b", "c"], cooldown_seconds=60.0)
    pool.mark_rate_limited(1)
    picks = [pool.get_key()[1] for _ in range(6)]
    assert 1 not in picks


def test_cooldown_expires_after_the_configured_duration():
    pool = KeyPool(keys=["a", "b"], cooldown_seconds=0.05)
    pool.mark_rate_limited(0)
    assert 0 not in [pool.get_key()[1] for _ in range(4)]
    time.sleep(0.06)
    picks = [pool.get_key()[1] for _ in range(4)]
    assert 0 in picks


def test_all_keys_cooling_down_still_returns_a_key_not_an_error():
    pool = KeyPool(keys=["a", "b"], cooldown_seconds=60.0)
    pool.mark_rate_limited(0)
    pool.mark_rate_limited(1)
    # Should not raise — falls back to the least-recently-cooled key.
    key, index = pool.get_key()
    assert index in (0, 1)


def test_mark_rate_limited_ignores_out_of_range_index():
    pool = KeyPool(keys=["a", "b"])
    pool.mark_rate_limited(99)  # should not raise
    picks = [pool.get_key()[1] for _ in range(4)]
    assert set(picks) == {0, 1}


# ---------------------------------------------------------------------------
# _KeyedGemini
# ---------------------------------------------------------------------------


def test_keyed_gemini_binds_the_given_key():
    model = _KeyedGemini(model="gemini-2.5-flash", bound_api_key="secret-123")
    assert model.bound_api_key == "secret-123"
    client = model.api_client
    assert client is not None


def test_keyed_gemini_defaults_to_empty_bound_key():
    model = _KeyedGemini(model="gemini-2.5-flash")
    assert model.bound_api_key == ""


def test_build_model_returns_model_and_index(monkeypatch):
    pool = KeyPool(keys=["only-key"])
    monkeypatch.setattr("core.llm_client._pool", pool)
    model, index = build_model("gemini-2.5-flash")
    assert isinstance(model, _KeyedGemini)
    assert model.bound_api_key == "only-key"
    assert index == 0


def test_get_key_pool_is_a_singleton(monkeypatch):
    # Settings is a frozen dataclass — don't monkeypatch its methods.
    # Rely on GOOGLE_API_KEYS already being set for this test run instead.
    monkeypatch.setattr("core.llm_client._pool", None)
    p1 = get_key_pool()
    p2 = get_key_pool()
    assert p1 is p2


# ---------------------------------------------------------------------------
# Rate-limit classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "429 Too Many Requests",
        "Error: RESOURCE_EXHAUSTED",
        "You have exceeded your quota",
        "rate limit exceeded, please retry later",
    ],
)
def test_is_rate_limit_error_detects_known_patterns(message):
    assert is_rate_limit_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "Connection timed out",
        "Invalid argument: missing field 'ticket'",
        "500 Internal Server Error",
    ],
)
def test_is_rate_limit_error_ignores_unrelated_errors(message):
    assert is_rate_limit_error(Exception(message)) is False
