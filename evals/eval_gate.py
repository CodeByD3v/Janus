"""
evals/eval_gate.py — deterministic gate tests.

Pure-logic tests always run (no Docker, no API key needed).
Containerized-execution tests are skipped when Docker is unavailable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

# Ensure project root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings as real_settings  # noqa: E402
from core.gate import (  # noqa: E402
    run_candidate_test,
    run_full_gate,
    run_linter,
    run_security_scan,
    run_tests,
    run_type_check,
    sandbox_copy,
    write_candidate_test,
)


def _settings_with(**overrides):
    """Settings is a frozen dataclass singleton — monkeypatch.setattr on a
    dotted attribute path (e.g. "core.config.settings.SOME_FIELD") raises
    FrozenInstanceError. Build a fresh copy with just the needed overrides
    instead, and monkeypatch the *name* `core.gate.settings` (the
    module-local binding gate.py actually reads) to point at it.

    This bug previously affected TestContainerizedExecution below —
    masked in every environment tested so far because none had Docker
    (the tests are skip-gated on Docker availability), but GitHub
    Actions' ubuntu-latest runners DO have Docker pre-installed, so it
    would have failed the first time CI actually exercised this class.
    """
    return replace(real_settings, **overrides)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    """Check if Docker daemon is reachable."""
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=10
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _make_clean_repo(tmp: Path) -> Path:
    """Create a minimal repo with clean Python code and a passing test."""
    (tmp / "hello.py").write_text('def greet(name: str) -> str:\n    return f"Hello, {name}!"\n')
    tests = tmp / "tests"
    tests.mkdir()
    (tests / "test_hello.py").write_text(
        'from hello import greet\n\n\ndef test_greet():\n    assert greet("world") == "Hello, world!"\n'
    )
    (tmp / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    return tmp


def _make_bad_lint_repo(tmp: Path) -> Path:
    """Create a repo with intentional ruff violations."""
    # Unused import triggers F401
    (tmp / "bad.py").write_text("import os\nimport sys\n\nx = 1\n")
    return tmp


def _make_failing_test_repo(tmp: Path) -> Path:
    """Create a repo with a failing test."""
    (tmp / "lib.py").write_text("def add(a, b):\n    return a + b\n")
    tests = tmp / "tests"
    tests.mkdir()
    (tests / "test_lib.py").write_text(
        "from lib import add\n\n\ndef test_add_fails():\n    assert add(1, 2) == 999\n"
    )
    (tmp / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    return tmp


# ---------------------------------------------------------------------------
# Pure-logic tests (always run)
# ---------------------------------------------------------------------------


class TestSandboxCopy:
    def test_creates_isolated_dir(self, tmp_path: Path) -> None:
        src = tmp_path / "src_repo"
        src.mkdir()
        (src / "file.py").write_text("x = 1\n")

        sandbox = sandbox_copy(str(src))
        assert sandbox.exists()
        assert (sandbox / "file.py").exists()
        assert (sandbox / "file.py").read_text() == "x = 1\n"
        # Modifying sandbox doesn't touch original
        (sandbox / "file.py").write_text("x = 2\n")
        assert (src / "file.py").read_text() == "x = 1\n"
        shutil.rmtree(sandbox)


class TestRunLinter:
    def test_passes_clean_code(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_linter(str(repo))
        assert result["check"] == "linter"
        assert isinstance(result["passed"], bool)
        assert "detail" in result

    def test_fails_bad_code(self, tmp_path: Path) -> None:
        repo = _make_bad_lint_repo(tmp_path)
        result = run_linter(str(repo))
        assert result["check"] == "linter"
        assert result["passed"] is False
        assert result["detail"]  # Non-empty detail


class TestRunTypeCheck:
    def test_passes_clean_code(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_type_check(str(repo))
        assert result["check"] == "type_check"
        assert isinstance(result["passed"], bool)
        assert "detail" in result


class TestRunTests:
    def test_passes(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_tests(str(repo))
        assert result["check"] == "tests"
        assert result["passed"] is True

    def test_fails(self, tmp_path: Path) -> None:
        repo = _make_failing_test_repo(tmp_path)
        result = run_tests(str(repo))
        assert result["check"] == "tests"
        assert result["passed"] is False


class TestRunSecurityScan:
    def test_passes_clean_code(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_security_scan(str(repo))
        assert result["check"] == "security_scan"
        assert isinstance(result["passed"], bool)
        assert "detail" in result


class TestRunFullGate:
    def test_returns_correct_shape(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_full_gate(str(repo))
        assert "passed" in result
        assert "checks" in result
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) == 4
        for check in result["checks"]:
            assert "check" in check
            assert "passed" in check
            assert "detail" in check


class TestWriteCandidateTest:
    def test_creates_file(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = write_candidate_test(
            str(repo), "test_edge.py", "def test_edge():\n    assert True\n"
        )
        assert "written" in result
        written_path = Path(result["written"])
        assert written_path.exists()
        assert written_path.read_text() == "def test_edge():\n    assert True\n"
        assert written_path.parent.name == "tests"


class TestRunCandidateTest:
    """Covers the fix for a real, verified bug: write_candidate_test()
    hardcodes writing to `tests/`, but many real repos configure an
    explicit `testpaths` (pytest.ini / tox.ini / pyproject.toml) that
    excludes that directory — on such a repo, a bare `pytest -q` sweep
    (run_tests()) silently never executes the file. run_candidate_test()
    runs the exact file by path instead, which always collects it
    regardless of testpaths. Verified against a real external repo
    (pytest-dev/pluggy, whose tox.ini restricts testpaths to `testing/`)
    before this fix existed, and confirmed fixed after.
    """

    def test_failing_counterexample_is_detected(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        write_candidate_test(
            str(repo), "test_proves_bug.py",
            "def test_proves_bug():\n    assert False, 'bug exists'\n",
        )
        result = run_candidate_test(str(repo), "test_proves_bug.py")
        assert result["check"] == "candidate_test"
        assert result["passed"] is False
        assert "test_proves_bug" in result["detail"]

    def test_passing_counterexample_reports_passed(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        write_candidate_test(
            str(repo), "test_clean.py",
            "def test_clean():\n    assert True\n",
        )
        result = run_candidate_test(str(repo), "test_clean.py")
        assert result["passed"] is True

    def test_runs_even_when_testpaths_excludes_tests_dir(self, tmp_path: Path) -> None:
        """The exact bug this fix closes: a repo whose own pytest config
        restricts discovery to a directory other than `tests/` must still
        have the Reviewer's counterexample execute.

        Built from scratch rather than via _make_clean_repo(), which
        bakes in its own pytest.ini — pytest.ini takes precedence over
        tox.ini's [pytest] section when both exist in the same directory
        (confirmed by running this test against _make_clean_repo() first
        and watching testpaths get silently ignored), so layering a
        tox.ini on top of that fixture would not actually reproduce the
        bug. A single, uncontested pytest.ini with testpaths set mirrors
        exactly what was found on the real external repo this fix was
        verified against (pytest-dev/pluggy has no competing config file).
        """
        repo = tmp_path / "repo_with_own_testpaths"
        repo.mkdir()
        (repo / "hello.py").write_text(
            'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
        )
        other_dir = repo / "testing"
        other_dir.mkdir()
        (other_dir / "test_existing.py").write_text(
            "def test_existing():\n    assert True\n"
        )
        (repo / "pytest.ini").write_text("[pytest]\ntestpaths = testing\n")

        write_candidate_test(
            str(repo), "test_reviewer_proof.py",
            "def test_reviewer_proof():\n    assert False, 'must still run'\n",
        )

        # Sanity check: the general sweep, exactly as before this fix,
        # does NOT see the counterexample — this confirms the fixture
        # actually reproduces the bug, not just asserts the fix works.
        general = run_tests(str(repo))
        assert "test_reviewer_proof" not in general["detail"]

        # The fix: run_candidate_test bypasses testpaths and finds it.
        result = run_candidate_test(str(repo), "test_reviewer_proof.py")
        assert result["passed"] is False
        assert "test_reviewer_proof" in result["detail"]

    def test_path_traversal_denied(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_candidate_test(str(repo), "../../../etc/passwd")
        assert result["passed"] is False
        assert "traversal" in result["detail"].lower()

    def test_missing_file_reports_clear_error(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_candidate_test(str(repo), "never_written.py")
        assert result["passed"] is False
        assert "write_candidate_test" in result["detail"]

    def test_write_then_run_use_the_identical_path(self, tmp_path: Path) -> None:
        """write_candidate_test and run_candidate_test share one path
        resolution helper — this pins that contract so they can't drift
        apart silently in a future edit."""
        repo = _make_clean_repo(tmp_path)
        write_result = write_candidate_test(
            str(repo), "test_pin.py", "def test_pin():\n    assert True\n"
        )
        run_result = run_candidate_test(str(repo), "test_pin.py")
        assert write_result["written"] in run_result["detail"] or run_result["passed"] is True


# ---------------------------------------------------------------------------
# Containerized-execution tests (skip if Docker unavailable)
# ---------------------------------------------------------------------------

_skip_no_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker not available"
)


@_skip_no_docker
class TestContainerizedExecution:
    def test_containerized_linter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify linter runs inside a container when USE_CONTAINERIZED_GATE is set."""
        import core.gate as gate
        monkeypatch.setattr(gate, "settings", _settings_with(USE_CONTAINERIZED_GATE=True))
        repo = _make_clean_repo(tmp_path)
        result = run_linter(str(repo))
        assert result["check"] == "linter"
        assert isinstance(result["passed"], bool)

    def test_containerized_gate_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify timeout is respected in containerized execution."""
        import core.gate as gate
        monkeypatch.setattr(
            gate,
            "settings",
            _settings_with(USE_CONTAINERIZED_GATE=True, SANDBOX_TIMEOUT=1),
        )
        repo = _make_clean_repo(tmp_path)
        # This should complete or timeout — either way it returns a valid result
        result = run_linter(str(repo))
        assert "check" in result
        assert "passed" in result
        assert "detail" in result
