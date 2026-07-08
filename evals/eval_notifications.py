"""
evals/eval_notifications.py — PR comment / webhook notification tests
(GAP 17 / TASK 18).

All HTTP calls are mocked — these tests never touch the real network,
and never need a real GITHUB_TOKEN or webhook endpoint.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.notifications import (  # noqa: E402
    format_debate_summary,
    notify_debate_outcome,
    post_github_pr_comment,
    post_webhook,
)

from core.config import settings as real_settings  # noqa: E402


def _settings_with(**overrides):
    """Settings is a frozen dataclass singleton — monkeypatch.setattr can't
    mutate a field on it directly (raises FrozenInstanceError). Build a
    fresh copy with just the needed overrides instead, and monkeypatch the
    *name* `core.notifications.settings` to point at that copy."""
    return replace(real_settings, **overrides)


# ---------------------------------------------------------------------------
# format_debate_summary
# ---------------------------------------------------------------------------


def test_summary_reports_merged_verdict():
    summary = format_debate_summary("d1", True, [], None)
    assert "Merged" in summary
    assert "d1" in summary


def test_summary_reports_rejected_verdict():
    summary = format_debate_summary("d1", False, [], None)
    assert "Rejected" in summary


def test_summary_includes_round_details():
    rounds = [
        {
            "round_num": 1,
            "reviewer_text": "Found a null pointer issue in average_price.",
            "gate_result": {"passed": False},
            "stop_reason": None,
        },
        {
            "round_num": 2,
            "reviewer_text": "No further issues found.",
            "gate_result": {"passed": True},
            "stop_reason": "reviewer_satisfied",
        },
    ]
    summary = format_debate_summary("d2", True, rounds, None)
    assert "Round 1" in summary
    assert "Round 2" in summary
    assert "null pointer" in summary
    assert "reviewer_satisfied" in summary


def test_summary_truncates_long_reviewer_text():
    long_text = "x" * 1000
    rounds = [{"round_num": 1, "reviewer_text": long_text, "gate_result": {}}]
    summary = format_debate_summary("d3", False, rounds, None)
    assert "…" in summary
    assert len(summary) < len(long_text) + 200


def test_summary_includes_final_gate_checks():
    final_gate = {
        "checks": [
            {"check": "linter", "passed": True},
            {"check": "tests", "passed": False},
        ]
    }
    summary = format_debate_summary("d4", False, [], final_gate)
    assert "linter" in summary
    assert "tests" in summary


# ---------------------------------------------------------------------------
# post_github_pr_comment
# ---------------------------------------------------------------------------


def test_pr_comment_skipped_without_token(monkeypatch):
    monkeypatch.setattr("core.notifications.settings", _settings_with(GITHUB_TOKEN=""))
    result = post_github_pr_comment("owner/repo", 42, "body text")
    assert result is False


def test_pr_comment_posts_with_correct_url_and_auth(monkeypatch):
    monkeypatch.setattr("core.notifications.settings", _settings_with(GITHUB_TOKEN="fake-token"))
    mock_response = MagicMock(status_code=201, text="")

    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        result = post_github_pr_comment("owner/repo", 42, "critique body")

    assert result is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.github.com/repos/owner/repo/issues/42/comments"
    assert kwargs["json"] == {"body": "critique body"}
    assert kwargs["headers"]["Authorization"] == "Bearer fake-token"


def test_pr_comment_returns_false_on_non_2xx(monkeypatch):
    monkeypatch.setattr("core.notifications.settings", _settings_with(GITHUB_TOKEN="fake-token"))
    mock_response = MagicMock(status_code=403, text="Forbidden")

    with patch("core.notifications.requests.post", return_value=mock_response):
        result = post_github_pr_comment("owner/repo", 42, "body")

    assert result is False


def test_pr_comment_never_raises_on_network_error(monkeypatch):
    import requests as real_requests

    monkeypatch.setattr("core.notifications.settings", _settings_with(GITHUB_TOKEN="fake-token"))
    with patch(
        "core.notifications.requests.post",
        side_effect=real_requests.exceptions.ConnectionError("boom"),
    ):
        result = post_github_pr_comment("owner/repo", 42, "body")

    assert result is False


# ---------------------------------------------------------------------------
# post_webhook
# ---------------------------------------------------------------------------


def test_webhook_posts_payload():
    mock_response = MagicMock(status_code=200, text="")
    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        result = post_webhook("https://example.com/hook", {"debate_id": "d1"})

    assert result is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://example.com/hook"
    assert kwargs["json"] == {"debate_id": "d1"}


def test_webhook_never_raises_on_network_error():
    import requests as real_requests

    with patch(
        "core.notifications.requests.post",
        side_effect=real_requests.exceptions.Timeout("timed out"),
    ):
        result = post_webhook("https://example.com/hook", {})

    assert result is False


# ---------------------------------------------------------------------------
# notify_debate_outcome — the actual GAP 17 contract
# ---------------------------------------------------------------------------


def test_notify_is_a_noop_with_nothing_configured(monkeypatch):
    """A debate with no PR reference and no webhook must behave exactly
    as it did before this feature existed — no HTTP calls at all."""
    monkeypatch.setattr(
        "core.notifications.settings",
        _settings_with(DEFAULT_WEBHOOK_URL="", GITHUB_TOKEN=""),
    )
    with patch("core.notifications.requests.post") as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=True,
            rounds=[],
            final_gate=None,
            pr_repo=None,
            pr_number=None,
            webhook_url=None,
        )
    mock_post.assert_not_called()


def test_notify_posts_pr_comment_when_pr_reference_given(monkeypatch):
    monkeypatch.setattr(
        "core.notifications.settings",
        _settings_with(GITHUB_TOKEN="fake-token", DEFAULT_WEBHOOK_URL=""),
    )
    mock_response = MagicMock(status_code=201, text="")

    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=True,
            rounds=[],
            final_gate=None,
            pr_repo="owner/repo",
            pr_number=7,
            webhook_url=None,
        )

    mock_post.assert_called_once()
    assert "issues/7/comments" in mock_post.call_args[0][0]


def test_notify_posts_webhook_when_url_given():
    mock_response = MagicMock(status_code=200, text="")
    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=False,
            rounds=[],
            final_gate=None,
            pr_repo=None,
            pr_number=None,
            webhook_url="https://example.com/hook",
        )

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "https://example.com/hook"


def test_notify_falls_back_to_default_webhook(monkeypatch):
    monkeypatch.setattr(
        "core.notifications.settings",
        _settings_with(DEFAULT_WEBHOOK_URL="https://default.example.com/hook"),
    )
    mock_response = MagicMock(status_code=200, text="")
    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=True,
            rounds=[],
            final_gate=None,
            pr_repo=None,
            pr_number=None,
            webhook_url=None,
        )

    mock_post.assert_called_once()
    assert mock_post.call_args[0][0] == "https://default.example.com/hook"


def test_notify_fires_both_when_both_configured(monkeypatch):
    monkeypatch.setattr("core.notifications.settings", _settings_with(GITHUB_TOKEN="fake-token"))
    mock_response = MagicMock(status_code=200, text="")
    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=True,
            rounds=[],
            final_gate=None,
            pr_repo="owner/repo",
            pr_number=3,
            webhook_url="https://example.com/hook",
        )

    assert mock_post.call_count == 2


def test_notify_requires_both_pr_repo_and_pr_number(monkeypatch):
    """Only pr_repo set, no pr_number — must NOT attempt a PR comment
    (mirrors api/schemas.py's cross-field validation, defensively)."""
    monkeypatch.setattr(
        "core.notifications.settings",
        _settings_with(GITHUB_TOKEN="fake-token", DEFAULT_WEBHOOK_URL=""),
    )
    with patch("core.notifications.requests.post") as mock_post:
        notify_debate_outcome(
            debate_id="d1",
            merged=True,
            rounds=[],
            final_gate=None,
            pr_repo="owner/repo",
            pr_number=None,
            webhook_url=None,
        )
    mock_post.assert_not_called()
