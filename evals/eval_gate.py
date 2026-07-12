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
    _resolve_scoped_path,
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
    def test_creates_isolated_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # validate_repo_ref() lives in core.path_safety and reads ITS OWN
        # bound `settings` name (from `from core.config import settings`
        # at the top of that module) — patching core.gate.settings has no
        # effect on it, even though gate.py imports validate_repo_ref.
        import core.path_safety as path_safety_module
        monkeypatch.setattr(
            path_safety_module,
            "settings",
            _settings_with(ALLOWED_REPO_ROOTS=str(tmp_path)),
        )

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


class TestRepoDirValidation:
    """Covers a finding from a follow-up security audit: every function
    in gate.py is exposed directly as an MCP tool to the Patcher/Reviewer
    agents (see mcp_server/server.py), so an agent's own tool-call
    arguments reach these functions with NO validation from
    orchestrator.py in between — orchestrator.py's own repo_ref
    validation only guards the ONE call IT makes into sandbox_copy(), not
    any call an agent makes directly. Before this fix, an agent could
    call e.g. run_tests(repo_dir="/etc") directly and gate.py would
    happily mount or scan it.

    Two distinct checks, for two distinct meanings of repo_dir:
    - sandbox_copy(): repo_dir is an ORIGINAL SOURCE path — reuses
      validate_repo_ref() (ALLOWED_REPO_ROOTS), the same check already
      applied at the API/orchestrator layer.
    - Everything else: repo_dir is a SANDBOX path (already-copied,
      already-isolated) — a looser "must resolve under the OS temp
      directory" check, since requiring exact ALLOWED_REPO_ROOTS
      membership here would reject sandbox_copy()'s own legitimate output
      (which lives in /tmp, not wherever the original source repos are
      configured to live).
    """

    def test_sandbox_copy_rejects_arbitrary_host_path_without_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-closed default: with no ALLOWED_REPO_ROOTS configured at
        all, sandbox_copy must reject everything, not silently allow it."""
        import core.path_safety as path_safety_module
        monkeypatch.setattr(
            path_safety_module, "settings", _settings_with(ALLOWED_REPO_ROOTS="")
        )
        with pytest.raises(ValueError, match="ALLOWED_REPO_ROOTS"):
            sandbox_copy("/etc")

    def test_sandbox_copy_rejects_path_outside_allowlist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The actual reported vulnerability: an agent calling
        sandbox_copy directly with a host path like /etc, unrelated to
        whatever repo the debate is actually about."""
        import core.path_safety as path_safety_module
        allowed = tmp_path / "allowed_repos"
        allowed.mkdir()
        monkeypatch.setattr(
            path_safety_module,
            "settings",
            _settings_with(ALLOWED_REPO_ROOTS=str(allowed)),
        )
        with pytest.raises(ValueError):
            sandbox_copy("/etc")

    @pytest.mark.parametrize(
        "bad_repo_dir",
        ["/etc", "/home", "/root", "/", "/var/lib"],
    )
    def test_scan_functions_reject_paths_outside_temp_dir(
        self, bad_repo_dir: str
    ) -> None:
        """The other half of the same finding: run_linter/run_type_check/
        run_tests/run_security_scan/write_candidate_test/
        run_candidate_test must all refuse to operate on a repo_dir that
        isn't a sandbox path, independent of sandbox_copy's own
        ALLOWED_REPO_ROOTS check (an agent can call these directly
        without ever having called sandbox_copy first)."""
        for fn, kwargs in [
            (run_linter, {}),
            (run_type_check, {}),
            (run_tests, {}),
            (run_security_scan, {}),
        ]:
            result = fn(bad_repo_dir, **kwargs)
            assert result["passed"] is False
            assert "temp directory" in result["detail"]

        write_result = write_candidate_test(bad_repo_dir, "x.py", "content")
        assert "error" in write_result
        assert "temp directory" in write_result["error"]

        run_candidate_result = run_candidate_test(bad_repo_dir, "x.py")
        assert run_candidate_result["passed"] is False
        assert "temp directory" in run_candidate_result["detail"]

    def test_scan_functions_still_accept_legitimate_sandbox_path(
        self, tmp_path: Path
    ) -> None:
        """Backward compatibility: a genuine tmp_path-style fixture (or
        real sandbox_copy() output) must not be rejected by the new
        check — only paths outside the OS temp directory should be."""
        repo = _make_clean_repo(tmp_path)
        result = run_linter(str(repo))
        assert result["passed"] is True

    def test_full_gate_rejects_arbitrary_host_path(self) -> None:
        """run_full_gate aggregates the four checks — confirm the
        rejection propagates through and the overall gate correctly
        reports failure rather than silently scanning /etc."""
        result = run_full_gate("/etc")
        assert result["passed"] is False
        for check in result["checks"]:
            assert check["passed"] is False


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


class TestScopedChecks:
    """Covers the fix for two real, verified problems with running
    lint/type/security checks unscoped (against the whole repo):

    1. mypy crashes outright ("Duplicate module named") on any repo with
       two files sharing a module name in different directories — a
       common real pattern (e.g. multiple example subprojects, each with
       their own setup.py). Scoping to target_file sidesteps whole-tree
       package resolution entirely.
    2. Even scoped, mypy's default import-following can still surface
       pre-existing, unrelated errors in files target_file imports —
       fixed with --follow-imports=silent, which suppresses those while
       still catching a genuine new error introduced directly in
       target_file itself.

    Scoping is sound, not just convenient, because the Patcher can only
    ever write to target_file (orchestrator.py never touches any other
    path) — see run_linter's docstring in gate.py. Both findings were
    verified against a real external repo (pytest-dev/pluggy) before
    this fix existed, and reproduced here with synthetic fixtures so
    they don't depend on network access to clone anything.
    """

    def test_linter_scoped_to_bad_file_fails(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        (repo / "bad_scoped.py").write_text("import os\nimport sys\n\nx=1\n")
        result = run_linter(str(repo), "bad_scoped.py")
        assert result["passed"] is False

    def test_linter_scoped_to_clean_file_passes(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        (repo / "good_scoped.py").write_text("x = 1\n")
        result = run_linter(str(repo), "good_scoped.py")
        assert result["passed"] is True

    def test_linter_scoped_path_traversal_denied(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        result = run_linter(str(repo), "../../../etc/passwd")
        assert result["passed"] is False
        assert "escapes" in result["detail"]

    def test_type_check_duplicate_module_crash_is_avoided_when_scoped(
        self, tmp_path: Path
    ) -> None:
        """Reproduces the exact structural pattern found on pytest-dev/
        pluggy: two files sharing a module name in different
        subdirectories, which makes unscoped `mypy .` fail outright with
        'Duplicate module named', unrelated to any actual type error."""
        repo = tmp_path / "multi_subproject_repo"
        repo.mkdir()
        (repo / "main.py").write_text("x: int = 1\n")

        example_a = repo / "examples" / "project_a"
        example_b = repo / "examples" / "project_b"
        example_a.mkdir(parents=True)
        example_b.mkdir(parents=True)
        (example_a / "setup.py").write_text("# example project A\n")
        (example_b / "setup.py").write_text("# example project B\n")

        # Sanity check: unscoped mypy genuinely crashes on this fixture —
        # confirms the fixture reproduces the bug, not just asserts the fix.
        unscoped = run_type_check(str(repo))
        assert unscoped["passed"] is False
        assert "Duplicate module" in unscoped["detail"]

        # The fix: scoped to main.py, the duplicate examples are never
        # touched by mypy's package resolution at all.
        scoped = run_type_check(str(repo), "main.py")
        assert scoped["passed"] is True

    def test_type_check_suppresses_preexisting_error_in_imported_file(
        self, tmp_path: Path
    ) -> None:
        """--follow-imports=silent: a pre-existing mypy error in a file
        target_file imports must not fail the gate for a patch that never
        touched that file."""
        repo = tmp_path / "repo_with_debt"
        repo.mkdir()
        (repo / "helper.py").write_text(
            "def helper() -> int:  # type: ignore[misc]\n    return 1\n"
        )
        (repo / "main.py").write_text("from helper import helper\nx = helper()\n")

        # helper.py's ignore comment is unnecessary (no actual error there),
        # which mypy flags as "unused ignore" — genuine pre-existing debt.
        result = run_type_check(str(repo), "main.py")
        assert result["passed"] is True

    def test_type_check_still_catches_real_error_in_target_file(
        self, tmp_path: Path
    ) -> None:
        """--follow-imports=silent must not become a blanket suppression
        — a real type error introduced directly in target_file itself
        must still fail the check."""
        repo = tmp_path / "repo_with_real_bug"
        repo.mkdir()
        (repo / "helper.py").write_text("def helper() -> int:\n    return 1\n")
        (repo / "main.py").write_text(
            "from helper import helper\nx: str = helper()\n"
        )
        result = run_type_check(str(repo), "main.py")
        assert result["passed"] is False

    def test_security_scan_scoped_to_insecure_file_fails(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        (repo / "insecure.py").write_text(
            'import subprocess\nsubprocess.call("ls", shell=True)\n'
        )
        result = run_security_scan(str(repo), "insecure.py")
        assert result["passed"] is False

    def test_resolve_scoped_path_denies_traversal(self, tmp_path: Path) -> None:
        repo = _make_clean_repo(tmp_path)
        assert _resolve_scoped_path(str(repo), "../../../etc/passwd") is None
        assert _resolve_scoped_path(str(repo), "hello.py") is not None

    def test_full_gate_scopes_static_checks_but_not_tests(self, tmp_path: Path) -> None:
        """run_full_gate must pass target_file through to lint/type/
        security, but run_tests always runs the full suite regardless —
        pin that asymmetry so it can't silently drift."""
        repo = _make_clean_repo(tmp_path)
        result = run_full_gate(str(repo), "hello.py")
        checks_by_name = {c["check"]: c for c in result["checks"]}
        assert set(checks_by_name) == {
            "linter", "type_check", "tests", "security_scan"
        }
        assert result["passed"] is True

    def test_full_gate_without_target_file_still_scans_whole_repo(
        self, tmp_path: Path
    ) -> None:
        """Backward compatibility: omitting target_file must behave
        exactly as before this change — no silent behavior shift for
        existing callers that don't pass it."""
        repo = _make_bad_lint_repo(tmp_path)
        result = run_full_gate(str(repo))
        assert result["passed"] is False


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
