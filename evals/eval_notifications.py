"""
evals/eval_notifications.py — PR comment / webhook notification tests
(GAP 17 / TASK 18).

All HTTP calls are mocked — these tests never touch the real network,
and never need a real GITHUB_TOKEN or webhook endpoint. This includes
DNS resolution: post_webhook() now resolves the destination hostname to
check for SSRF (see core/notifications.py's module docstring) — the
autouse `mock_safe_dns` fixture below patches socket.getaddrinfo to a
deterministic, known-safe public IP by default, so existing tests using
a placeholder URL like https://example.com/hook stay network-free rather
than silently depending on live DNS resolution actually succeeding.
Tests that specifically exercise the SSRF rejection path override this
mock themselves to return a private/internal IP instead.
"""

from __future__ import annotations

import socket
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.notifications import (  # noqa: E402
    _is_safe_webhook_url,
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


def _fake_addrinfo(ip: str):
    """Build a socket.getaddrinfo-shaped return value for a single IP,
    matching the (family, type, proto, canonname, sockaddr) tuple shape
    _is_safe_webhook_url actually reads (only sockaddr[0] is used)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.fixture(autouse=True)
def mock_safe_dns(monkeypatch: pytest.MonkeyPatch):
    """Default DNS mock: any hostname resolves to a known-safe public IP
    (8.8.8.8, Google's public DNS — verified classified as public/global
    by Python's ipaddress module, not private/reserved). Individual tests
    override this via their own monkeypatch.setattr call to test the
    rejection path instead.

    Note: 203.0.113.0/24 (IANA's TEST-NET-3 documentation range) was
    considered for this fixture and rejected after verifying it's
    actually classified as is_private=True by ipaddress — using it here
    would have made every test relying on this fixture fail.
    """
    monkeypatch.setattr(
        "core.notifications.socket.getaddrinfo",
        lambda *a, **kw: _fake_addrinfo("8.8.8.8"),
    )


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
# SSRF protection — a real, verified finding from a follow-up security
# audit: post_webhook() previously made an unrestricted requests.post(url)
# with no destination check at all, so any tenant could set webhook_url to
# an internal/cloud-metadata address (e.g. 169.254.169.254) and have
# Janus's own backend make requests against infrastructure the tenant has
# no direct network access to. _is_safe_webhook_url() closes this by
# resolving the hostname and rejecting private/loopback/link-local/
# reserved/multicast/unspecified addresses before any request is made.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip,label",
    [
        ("169.254.169.254", "AWS/GCP cloud metadata endpoint — the exact attack described"),
        ("127.0.0.1", "loopback"),
        ("10.0.0.5", "RFC1918 private"),
        ("172.16.0.1", "RFC1918 private"),
        ("192.168.1.1", "RFC1918 private"),
        ("0.0.0.0", "unspecified"),
        ("::1", "IPv6 loopback"),
        ("fe80::1", "IPv6 link-local"),
    ],
)
def test_is_safe_webhook_url_rejects_internal_addresses(monkeypatch, ip, label):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(
        "core.notifications.socket.getaddrinfo",
        lambda *a, **kw: [(family, socket.SOCK_STREAM, 6, "", (ip, 0))],
    )
    safe, reason = _is_safe_webhook_url("http://internal.example/hook")
    assert safe is False, f"{label} ({ip}) should have been rejected"
    assert ip in reason


def test_is_safe_webhook_url_accepts_public_address(monkeypatch):
    monkeypatch.setattr(
        "core.notifications.socket.getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))],
    )
    safe, reason = _is_safe_webhook_url("https://example.com/hook")
    assert safe is True
    assert reason == ""


def test_is_safe_webhook_url_rejects_if_any_resolved_address_is_unsafe(monkeypatch):
    """A hostname can have multiple A/AAAA records — every one must be
    checked, not just the first, since an attacker-influenced DNS
    response could list a safe address first and an unsafe one second."""
    monkeypatch.setattr(
        "core.notifications.socket.getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0)),
        ],
    )
    safe, reason = _is_safe_webhook_url("http://multi-record.example/hook")
    assert safe is False


def test_is_safe_webhook_url_rejects_unresolvable_hostname(monkeypatch):
    def _raise(*a, **kw):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr("core.notifications.socket.getaddrinfo", _raise)
    safe, reason = _is_safe_webhook_url("http://this-does-not-resolve.invalid/hook")
    assert safe is False
    assert "resolve" in reason


def test_is_safe_webhook_url_rejects_non_http_scheme():
    safe, reason = _is_safe_webhook_url("ftp://example.com/hook")
    assert safe is False
    assert "scheme" in reason


def test_is_safe_webhook_url_rejects_url_with_no_hostname():
    safe, reason = _is_safe_webhook_url("http:///path-only")
    assert safe is False


def test_post_webhook_blocks_before_making_any_request(monkeypatch):
    """The actual end-to-end contract: post_webhook must never call
    requests.post at all for an unsafe URL — not attempt the request and
    then report failure, but refuse outright."""
    monkeypatch.setattr(
        "core.notifications.socket.getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))],
    )
    with patch("core.notifications.requests.post") as mock_post:
        result = post_webhook(
            "http://attacker-controlled.example/hook", {"debate_id": "d1"}
        )
    assert result is False
    mock_post.assert_not_called()


def test_post_webhook_still_works_for_a_safe_url():
    """Backward compatibility: the SSRF check must not break the
    legitimate, already-tested happy path above — pinned again here
    explicitly alongside the new SSRF tests for clarity."""
    mock_response = MagicMock(status_code=200, text="")
    with patch("core.notifications.requests.post", return_value=mock_response) as mock_post:
        result = post_webhook("https://example.com/hook", {"debate_id": "d1"})
    assert result is True
    mock_post.assert_called_once()


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
