"""
evals/eval_repo_context.py — repository-context retrieval tests (GAP 14).

Pure-logic tests over a temporary repo fixture. No Docker, no API key,
no network needed — everything here reads local files and (optionally)
runs `git` against a temp directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.repo_context import (  # noqa: E402
    format_repo_context_for_prompt,
    retrieve_repo_context,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_repo():
    """A small temp repo: a target file with a function called from
    elsewhere, a tests/ dir with an existing convention, and (optionally)
    git history with a tagged bug-fix commit."""
    tmp = Path(tempfile.mkdtemp(prefix="repo_context_test_"))
    try:
        target = tmp / "inventory.py"
        target.write_text(
            "def average_price(items):\n"
            "    if not items:\n"
            "        return 0.0\n"
            "    return sum(i.price for i in items) / len(items)\n"
        )

        caller = tmp / "reports.py"
        caller.write_text(
            "from inventory import average_price\n\n"
            "def summarize(items):\n"
            "    return average_price(items)\n"
        )

        tests_dir = tmp / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_reports.py").write_text(
            "import pytest\n\ndef test_summarize_empty():\n    assert True\n"
        )

        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def temp_git_repo(temp_repo):
    """Same fixture, but a real git repo with a tagged bug-fix commit
    touching the target file, for the git-history signal."""

    def _git(*args):
        subprocess.run(["git", *args], cwd=temp_repo, capture_output=True, text=True)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", ".")
    _git("commit", "-q", "-m", "initial commit")

    target = temp_repo / "inventory.py"
    target.write_text(target.read_text() + "\n# adjusted rounding\n")
    _git("add", "inventory.py")
    _git("commit", "-q", "-m", "fix: average_price rounding bug")

    return temp_repo


# ---------------------------------------------------------------------------
# Call graph
# ---------------------------------------------------------------------------


def test_call_graph_finds_caller_in_another_file(temp_repo):
    current_code = (temp_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_repo), "inventory.py", current_code)

    assert "average_price" in context["call_graph"]["defined_here"]
    assert "reports.py" in context["call_graph"]["callers"]


def test_call_graph_handles_unparseable_code(temp_repo):
    context = retrieve_repo_context(str(temp_repo), "inventory.py", "def broken(:\n    pass")
    assert context["call_graph"] == {
        "defined_here": [],
        "called_elsewhere": [],
        "callers": [],
    }


# ---------------------------------------------------------------------------
# Git history
# ---------------------------------------------------------------------------


def test_prior_fixes_empty_when_not_a_git_repo(temp_repo):
    current_code = (temp_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_repo), "inventory.py", current_code)
    assert context["prior_fixes"] == []


def test_prior_fixes_found_in_git_repo(temp_git_repo):
    current_code = (temp_git_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_git_repo), "inventory.py", current_code)
    assert len(context["prior_fixes"]) == 1
    assert "fix" in context["prior_fixes"][0]["message"].lower()


# ---------------------------------------------------------------------------
# Test conventions
# ---------------------------------------------------------------------------


def test_test_conventions_excludes_target_files_own_tests(temp_repo):
    (temp_repo / "tests" / "test_inventory.py").write_text(
        "def test_average_price():\n    assert True\n"
    )
    current_code = (temp_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_repo), "inventory.py", current_code)

    sampled_names = [s.split(":", 1)[0] for s in context["test_conventions"]]
    assert "test_reports.py" in sampled_names
    assert "test_inventory.py" not in sampled_names


def test_test_conventions_empty_when_no_tests_dir(temp_repo):
    shutil.rmtree(temp_repo / "tests")
    current_code = (temp_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_repo), "inventory.py", current_code)
    assert context["test_conventions"] == []


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def test_format_repo_context_includes_all_signals(temp_git_repo):
    current_code = (temp_git_repo / "inventory.py").read_text()
    context = retrieve_repo_context(str(temp_git_repo), "inventory.py", current_code)
    rendered = format_repo_context_for_prompt(context)

    assert "reports.py" in rendered
    assert "fix" in rendered.lower()
    assert "test_reports.py" in rendered


def test_format_repo_context_handles_empty_signals():
    rendered = format_repo_context_for_prompt(
        {"call_graph": {}, "prior_fixes": [], "test_conventions": []}
    )
    assert rendered == "No repository context available."


def test_format_repo_context_handles_empty_dict():
    assert format_repo_context_for_prompt({}) == "No repository context available."
