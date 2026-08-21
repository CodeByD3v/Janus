"""
evals/eval_auto_merge.py — Phase 7 auto-merge tests.

Pure-logic tests — no real GitHub API calls. All httpx calls are mocked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.auto_merge import should_auto_merge, execute_auto_merge  # noqa: E402
from core.repo_config import RepoConfig  # noqa: E402


# ---------------------------------------------------------------------------
# should_auto_merge — policy checks
# ---------------------------------------------------------------------------


def _config(**overrides) -> RepoConfig:
    """Build a RepoConfig with auto_merge=True and optional overrides."""
    defaults = {"auto_merge": True}
    defaults.update(overrides)
    return RepoConfig(**defaults)


def test_auto_merge_requires_opt_in():
    """auto_merge=False in janus.yaml → never auto-merge."""
    rc = RepoConfig(auto_merge=False)
    assert should_auto_merge(rc, merged=True, needs_human_review=False) is False


def test_auto_merge_requires_gate_passed():
    """merged=False → never auto-merge even if repo opts in."""
    rc = _config()
    assert should_auto_merge(rc, merged=False, needs_human_review=False) is False


def test_auto_merge_blocked_by_human_review():
    """needs_human_review=True (INCONCLUSIVE) → never auto-merge."""
    rc = _config()
    assert should_auto_merge(rc, merged=True, needs_human_review=True) is False


def test_auto_merge_happy_path():
    """All conditions met → auto-merge allowed."""
    rc = _config()
    assert should_auto_merge(rc, merged=True, needs_human_review=False) is True


def test_auto_merge_branch_pattern_match():
    """Branch matches an allowed pattern → allowed."""
    rc = _config(auto_merge_branches=["dependabot/*", "renovate/*"])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_branch="dependabot/npm_and_yarn/axios-1.7.0",
    ) is True


def test_auto_merge_branch_pattern_no_match():
    """Branch doesn't match any allowed pattern → blocked."""
    rc = _config(auto_merge_branches=["dependabot/*"])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_branch="feature/new-ui",
    ) is False


def test_auto_merge_branch_empty_list_allows_all():
    """No branch restrictions → all branches allowed."""
    rc = _config(auto_merge_branches=[])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_branch="feature/anything",
    ) is True


def test_auto_merge_author_match():
    """Author is in the trusted list → allowed."""
    rc = _config(auto_merge_authors=["dependabot[bot]", "renovate[bot]"])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_author="dependabot[bot]",
    ) is True


def test_auto_merge_author_no_match():
    """Author not in trusted list → blocked."""
    rc = _config(auto_merge_authors=["dependabot[bot]"])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_author="untrusted-user",
    ) is False


def test_auto_merge_author_empty_list_allows_all():
    """No author restrictions → all authors allowed."""
    rc = _config(auto_merge_authors=[])
    assert should_auto_merge(
        rc, merged=True, needs_human_review=False,
        pr_author="anyone",
    ) is True


# ---------------------------------------------------------------------------
# execute_auto_merge — GitHub API interaction (mocked)
# ---------------------------------------------------------------------------


@patch("core.auto_merge.httpx.Client")
@patch("core.auto_merge.settings")
def test_execute_auto_merge_success(mock_settings, mock_client_cls):
    """Successful merge → returns True."""
    mock_settings.GITHUB_TOKEN = "test-token"
    mock_settings.GITHUB_API_URL = "https://api.github.com"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.put.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    assert execute_auto_merge("owner/repo", 42, commit_sha="abc123") is True
    mock_client.put.assert_called_once()


@patch("core.auto_merge.settings")
def test_execute_auto_merge_no_token(mock_settings):
    """No GITHUB_TOKEN → returns False without making API call."""
    mock_settings.GITHUB_TOKEN = ""
    assert execute_auto_merge("owner/repo", 42) is False


@patch("core.auto_merge.httpx.Client")
@patch("core.auto_merge.settings")
def test_execute_auto_merge_405_not_allowed(mock_settings, mock_client_cls):
    """405 (merge blocked) → returns False."""
    mock_settings.GITHUB_TOKEN = "test-token"
    mock_settings.GITHUB_API_URL = "https://api.github.com"

    mock_resp = MagicMock()
    mock_resp.status_code = 405
    mock_resp.text = "Branch protection prevents merge"
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.put.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    assert execute_auto_merge("owner/repo", 42) is False


@patch("core.auto_merge.httpx.Client")
@patch("core.auto_merge.settings")
def test_execute_auto_merge_409_sha_mismatch(mock_settings, mock_client_cls):
    """409 (SHA mismatch) → returns False."""
    mock_settings.GITHUB_TOKEN = "test-token"
    mock_settings.GITHUB_API_URL = "https://api.github.com"

    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.text = "Head branch was modified"
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.put.return_value = mock_resp
    mock_client_cls.return_value = mock_client

    assert execute_auto_merge("owner/repo", 42, commit_sha="stale") is False



def test_auto_merge_branch_allowlist_requires_branch_metadata():
    rc = _config(auto_merge_branches=["dependabot/*"])
    assert should_auto_merge(rc, merged=True, needs_human_review=False) is False


def test_auto_merge_author_allowlist_requires_author_metadata():
    rc = _config(auto_merge_authors=["dependabot[bot]"])
    assert should_auto_merge(rc, merged=True, needs_human_review=False) is False
