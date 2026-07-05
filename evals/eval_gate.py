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
from pathlib import Path

import pytest

# Ensure project root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gate import (  # noqa: E402
    run_full_gate,
    run_linter,
    run_security_scan,
    run_tests,
    run_type_check,
    sandbox_copy,
    write_candidate_test,
)


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
        monkeypatch.setattr("core.config.settings.USE_CONTAINERIZED_GATE", True)
        repo = _make_clean_repo(tmp_path)
        result = run_linter(str(repo))
        assert result["check"] == "linter"
        assert isinstance(result["passed"], bool)

    def test_containerized_gate_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify timeout is respected in containerized execution."""
        monkeypatch.setattr("core.config.settings.USE_CONTAINERIZED_GATE", True)
        monkeypatch.setattr("core.config.settings.SANDBOX_TIMEOUT", 1)  # 1 second timeout
        repo = _make_clean_repo(tmp_path)
        # This should complete or timeout — either way it returns a valid result
        result = run_linter(str(repo))
        assert "check" in result
        assert "passed" in result
        assert "detail" in result
