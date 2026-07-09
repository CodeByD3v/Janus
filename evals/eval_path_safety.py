"""
evals/eval_path_safety.py — repo_ref allowlist and target_file denylist
tests.

Covers the previously-undiscovered gap where repo_ref had NO validation
at all (any authenticated caller could point Janus at an arbitrary
filesystem path), plus the fast-fail target_file pre-check.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings as real_settings  # noqa: E402
import core.path_safety as path_safety_module  # noqa: E402
from core.path_safety import (  # noqa: E402
    looks_like_path_traversal,
    validate_repo_ref,
)


def _settings_with(**overrides):
    """Settings is a frozen dataclass singleton — build a fresh copy with
    just the needed overrides rather than mutating the real instance."""
    return replace(real_settings, **overrides)


@pytest.fixture
def allowed_root(tmp_path):
    """A temp directory to use as the sole allowed repo root."""
    d = tmp_path / "repos"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# validate_repo_ref — fail-closed default
# ---------------------------------------------------------------------------

def test_fails_closed_with_no_allowed_roots_configured(monkeypatch):
    monkeypatch.setattr(
        path_safety_module, "settings", _settings_with(ALLOWED_REPO_ROOTS="")
    )
    with pytest.raises(ValueError, match="no ALLOWED_REPO_ROOTS"):
        validate_repo_ref("anything")


# ---------------------------------------------------------------------------
# validate_repo_ref — allowlist enforcement
# ---------------------------------------------------------------------------

def test_accepts_a_path_under_the_allowed_root(monkeypatch, allowed_root):
    monkeypatch.setattr(
        path_safety_module,
        "settings",
        _settings_with(ALLOWED_REPO_ROOTS=str(allowed_root)),
    )
    subdir = allowed_root / "my-repo"
    subdir.mkdir()
    assert validate_repo_ref(str(subdir)) == str(subdir)


def test_rejects_an_arbitrary_path_outside_the_allowlist(monkeypatch, allowed_root):
    """The actual vulnerability this closes: repo_ref had no restriction
    at all, so any caller could point Janus at e.g. /etc."""
    monkeypatch.setattr(
        path_safety_module,
        "settings",
        _settings_with(ALLOWED_REPO_ROOTS=str(allowed_root)),
    )
    with pytest.raises(ValueError, match="allowed repository roots"):
        validate_repo_ref("/etc")


def test_rejects_traversal_that_escapes_the_allowed_root(monkeypatch, allowed_root):
    monkeypatch.setattr(
        path_safety_module,
        "settings",
        _settings_with(ALLOWED_REPO_ROOTS=str(allowed_root)),
    )
    escape_attempt = str(allowed_root / ".." / ".." / "etc")
    with pytest.raises(ValueError):
        validate_repo_ref(escape_attempt)


def test_accepts_a_path_under_any_of_multiple_allowed_roots(monkeypatch, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setattr(
        path_safety_module,
        "settings",
        _settings_with(ALLOWED_REPO_ROOTS=f"{root_a},{root_b}"),
    )
    target = root_b / "some-repo"
    target.mkdir()
    assert validate_repo_ref(str(target)) == str(target)


def test_symlink_escaping_the_allowed_root_is_caught(monkeypatch, allowed_root, tmp_path):
    """resolve() follows symlinks, so a symlink inside the allowed root
    that points outside it must still be rejected."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed_root / "escape-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported in this environment")

    monkeypatch.setattr(
        path_safety_module,
        "settings",
        _settings_with(ALLOWED_REPO_ROOTS=str(allowed_root)),
    )
    with pytest.raises(ValueError):
        validate_repo_ref(str(link))


# ---------------------------------------------------------------------------
# looks_like_path_traversal — target_file fast-fail denylist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("inventory.py", False),
        ("subdir/file.py", False),
        ("a/b/c.py", False),
        ("../etc/passwd", True),
        ("/etc/passwd", True),
        ("", True),
        ("   ", True),
        ("a/../../b", True),
        ("..", True),
    ],
)
def test_looks_like_path_traversal(value, expected):
    assert looks_like_path_traversal(value) is expected
