"""
gate.py — the deterministic verification gate.

This is the non-negotiable part of the Adversarial Code Review system.
No matter what the Patcher and Reviewer agents say to each other in
natural language, a candidate patch only merges if it passes every
check here. These functions are plain, testable Python — they don't
call any LLM — and they are the same functions exposed to the agents
as MCP tools in mcp_server/server.py.

Each function returns a structured dict: {"passed": bool, "detail": str}
so the debate loop and the LLM agents can reason about failures without
parsing raw stdout.

Execution isolation (GAP 1 fix):
When USE_CONTAINERIZED_GATE is true, all tool commands execute inside a
fresh Docker container with:
  - --network none (no network access)
  - --memory limit (default 512m)
  - --cpus limit (default 1)
  - --pids-limit (default 128)
  - --read-only on everything except the mounted repo dir and /tmp
This prevents an adversarial or broken patch from escaping the sandbox.
When Docker is unavailable (dev/CI), falls back to direct subprocess
execution with only a wall-clock timeout.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)


def _is_docker_available() -> bool:
    """Check if Docker is available on this host."""
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_direct(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Execute a command directly via subprocess (fallback mode)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT after {timeout}s running: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 1, f"TOOL NOT FOUND: {e}"


def _run_containerized(
    cmd: list[str], repo_dir: Path, timeout: int | None = None
) -> tuple[int, str]:
    """Execute a command inside a locked-down Docker container.

    The container:
    - Mounts repo_dir as /workspace (read-write)
    - Has no network access (--network none)
    - Has CPU, memory, and PID limits
    - Is read-only except /workspace and /tmp
    - Is automatically removed after execution
    """
    effective_timeout = timeout or settings.SANDBOX_TIMEOUT
    docker_cmd = [
        "docker", "run",
        "--rm",
        "--network", "none",
        "--memory", settings.SANDBOX_MEMORY_LIMIT,
        "--cpus", settings.SANDBOX_CPU_LIMIT,
        "--pids-limit", str(settings.SANDBOX_PID_LIMIT),
        "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        "-v", f"{repo_dir.resolve()}:/workspace:rw",
        "-w", "/workspace",
        settings.SANDBOX_IMAGE,
    ] + cmd

    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output.strip()
    except subprocess.TimeoutExpired:
        return 1, f"TIMEOUT after {effective_timeout}s running containerized: {' '.join(cmd)}"
    except FileNotFoundError:
        logger.error("docker_binary_not_found_but_containerized_gate_enabled")
        return 1, "GATE ERROR: Docker is required but not found. Failing securely."


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    """Execute a gate command, using containerized execution if configured."""
    if settings.USE_CONTAINERIZED_GATE:
        if not _is_docker_available():
            logger.error("docker_unavailable_but_containerized_gate_enabled")
            return 1, "GATE ERROR: Docker is required but unavailable. Failing securely."
        return _run_containerized(cmd, cwd, timeout)
    return _run_direct(cmd, cwd, timeout)


def sandbox_copy(repo_dir: str) -> Path:
    """Copy the repo into an isolated temp dir so agent-proposed edits
    never touch the real working tree until they pass the gate."""
    tmp = Path(tempfile.mkdtemp(prefix="adv_review_sandbox_"))
    shutil.copytree(repo_dir, tmp, dirs_exist_ok=True)
    logger.info("sandbox_created", source=repo_dir, sandbox=str(tmp))
    return tmp


def _resolve_scoped_path(repo_dir: str, target_file: str) -> Path | None:
    """Resolve target_file against repo_dir with traversal protection.
    Returns None if it would escape repo_dir.

    Needed because run_linter/run_type_check/run_security_scan are
    exposed directly as MCP tools to both the Patcher and Reviewer (see
    agents.py's tool_filters) — an agent could call these with an
    arbitrary target_file argument independent of whatever validation
    orchestrator.py already does upstream. Same defense-in-depth
    reasoning as _resolve_candidate_test_path, generalized (no hardcoded
    "tests" subdirectory this time, since these checks scope to whatever
    file the caller names, not always a test file).
    """
    repo_path = Path(repo_dir).resolve()
    target = (repo_path / target_file).resolve()
    if not target.is_relative_to(repo_path):
        return None
    return target


def run_linter(repo_dir: str, target_file: str | None = None) -> dict:
    """Run ruff (style + safety lint rules).

    Scoped to target_file when given (GAP: mypy/ruff/bandit previously
    always scanned the whole repo, surfacing pre-existing lint/type/
    security debt unrelated to the patch and failing the gate on repos
    that were never clean to begin with — verified on a real external
    repo, pytest-dev/pluggy). This scoping is not a compromise: the
    Patcher can only ever write to target_file (orchestrator.py's
    run_debate never touches any other path), so a patch cannot
    introduce a NEW lint/type/security issue anywhere else — scanning
    just target_file is complete, not partial, for these three checks
    specifically. run_tests is deliberately NOT scoped this way — see
    its own docstring for why.
    """
    if target_file:
        target = _resolve_scoped_path(repo_dir, target_file)
        if target is None:
            return {
                "check": "linter",
                "passed": False,
                "detail": "target_file escapes the sandbox — refusing to scan.",
            }
        code, out = _run(["ruff", "check", target_file], cwd=Path(repo_dir))
    else:
        code, out = _run(["ruff", "check", "."], cwd=Path(repo_dir))
    return {"check": "linter", "passed": code == 0, "detail": out or "clean"}


def run_type_check(repo_dir: str, target_file: str | None = None) -> dict:
    """Run mypy. Scoped to target_file when given — see run_linter's
    docstring for why this scoping is sound, not just convenient.

    This directly fixes a real, verified crash: `mypy --ignore-missing-
    imports .` treats the whole repo as one package tree and errors out
    ("Duplicate module named 'x'") on any repo with two files sharing a
    module name in different directories — a very common pattern (e.g.
    multiple example/subproject dirs each with their own setup.py).
    Scoping to a single explicit file sidesteps that whole-tree package
    resolution entirely; mypy checking one file was never the thing that
    broke. Verified against pytest-dev/pluggy, which has exactly this
    duplicate-module structure in its docs/examples/ directory.
    """
    if target_file:
        target = _resolve_scoped_path(repo_dir, target_file)
        if target is None:
            return {
                "check": "type_check",
                "passed": False,
                "detail": "target_file escapes the sandbox — refusing to scan.",
            }
        code, out = _run(
            ["mypy", "--ignore-missing-imports", target_file], cwd=Path(repo_dir)
        )
    else:
        code, out = _run(
            ["mypy", "--ignore-missing-imports", "."], cwd=Path(repo_dir)
        )
    return {"check": "type_check", "passed": code == 0, "detail": out or "clean"}


def run_tests(repo_dir: str) -> dict:
    """Run the existing (and any newly added) pytest suite.

    DELIBERATELY NOT scoped to target_file, unlike run_linter/
    run_type_check/run_security_scan above. Those three are static,
    per-file analyses where the Patcher's single-file write means
    scoping is complete (see run_linter's docstring). Test execution is
    different in kind: it's a RUNTIME check across the whole call graph,
    and a patch to target_file can absolutely break a test that exercises
    a different file entirely (the exact cross-file breakage
    repo_context.py's call-graph retrieval exists to help the Reviewer
    anticipate). Narrowing this to "just run tests for target_file" would
    require guessing a test-file naming convention — the same class of
    fragile assumption that caused the write_candidate_test/run_tests
    bug fixed earlier — and would silently stop catching genuine
    regressions in exchange for dodging pre-existing test debt.

    This means the "gate conflates pre-existing repo debt with
    patch-introduced regressions" problem, as verified on pytest-dev/
    pluggy (5 tests failing on its own unmodified main), is NOT solved by
    this scoping change for tests specifically — only for lint/type/
    security. Solving it for tests requires comparing against a baseline
    run of the unpatched repo, a materially different (and pricier)
    mechanism than scoping, and remains open.
    """
    code, out = _run(["pytest", "-q"], cwd=Path(repo_dir))
    return {"check": "tests", "passed": code == 0, "detail": out or "clean"}


def run_security_scan(repo_dir: str, target_file: str | None = None) -> dict:
    """Run bandit. Scoped to target_file when given — see run_linter's
    docstring for why this scoping is sound, not just convenient."""
    if target_file:
        target = _resolve_scoped_path(repo_dir, target_file)
        if target is None:
            return {
                "check": "security_scan",
                "passed": False,
                "detail": "target_file escapes the sandbox — refusing to scan.",
            }
        # No -r (recursive) or -x (exclude dir) flags here — both are
        # directory-scan concepts that don't apply to a single file.
        code, out = _run(["bandit", "-q", target_file], cwd=Path(repo_dir))
    else:
        code, out = _run(
            ["bandit", "-q", "-r", ".", "-x", "./tests"], cwd=Path(repo_dir)
        )
    return {"check": "security_scan", "passed": code == 0, "detail": out or "clean"}


def run_full_gate(repo_dir: str, target_file: str | None = None) -> dict:
    """Run all four checks. The patch only merges if every check passes.

    target_file, when given, scopes the three static checks (lint, type,
    security) to just that file — see run_linter's docstring for why
    that's sound given this system's architecture. run_tests always runs
    the full suite regardless, by design — see run_tests's docstring.
    """
    checks = [
        run_linter(repo_dir, target_file),
        run_type_check(repo_dir, target_file),
        run_tests(repo_dir),
        run_security_scan(repo_dir, target_file),
    ]
    passed = all(c["passed"] for c in checks)
    logger.info(
        "gate_result",
        repo_dir=repo_dir,
        target_file=target_file,
        passed=passed,
        checks={c["check"]: c["passed"] for c in checks},
    )
    return {
        "passed": passed,
        "checks": checks,
    }


def _resolve_candidate_test_path(repo_dir: str, filename: str) -> Path | None:
    """Resolve where a Reviewer-written counterexample test lives, with
    path-traversal protection. Returns None if filename tries to escape
    the sandbox. Shared by write_candidate_test and run_candidate_test so
    both agree on the exact same path — they must never drift apart, or
    "run the file I just wrote" silently runs the wrong file.
    """
    repo_path = Path(repo_dir).resolve()
    target = (repo_path / "tests" / filename).resolve()
    if not target.is_relative_to(repo_path):
        return None
    return target


def write_candidate_test(repo_dir: str, filename: str, content: str) -> dict:
    """Let the Reviewer materialize an executable counterexample as a real
    test file in the sandbox, so a critique becomes a concrete pass/fail
    signal instead of prose.

    IMPORTANT: writing this file does NOT guarantee it will be picked up
    by run_tests()'s general `pytest -q` sweep. Many real repos configure
    an explicit `testpaths` (in pytest.ini, tox.ini, or pyproject.toml)
    that restricts discovery to a different directory — on such a repo,
    a file written here can be silently skipped by run_tests() even
    though it exists on disk. Use run_candidate_test() (below) to verify
    THIS specific file actually executes and fails, rather than relying
    on run_tests() to have swept it up. This was found and fixed after
    verifying against a real repo (pytest-dev/pluggy) whose tox.ini
    restricts testpaths to `testing/` — a file written to `tests/` there
    never ran under a bare `pytest -q`, silently.
    """
    target = _resolve_candidate_test_path(repo_dir, filename)
    if target is None:
        return {"error": "Path traversal denied: target must be inside the sandbox"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    logger.info("candidate_test_written", target=str(target))
    return {"written": str(target)}


def run_candidate_test(repo_dir: str, filename: str) -> dict:
    """Run ONE specific Reviewer-written counterexample test directly by
    exact file path, instead of relying on run_tests()'s repo-wide
    `pytest -q` sweep to happen to discover it.

    This matters because pytest only applies its own `testpaths` config
    (from pytest.ini / tox.ini / pyproject.toml) when invoked with NO
    explicit path arguments. Passing an exact file path on the command
    line — which this function does — always collects that file
    regardless of testpaths, sidestepping the exact failure mode
    documented in write_candidate_test()'s docstring above.

    filename must be the same value passed to write_candidate_test() —
    both resolve through the identical path helper, so "run the file I
    just wrote" can never silently target a different file.

    Returns the same {"check", "passed", "detail"} shape as the other
    run_* functions, so it slots into the same reasoning/logging
    conventions the Reviewer already uses for run_tests/run_linter/etc.
    """
    target = _resolve_candidate_test_path(repo_dir, filename)
    if target is None:
        return {
            "check": "candidate_test",
            "passed": False,
            "detail": "Path traversal denied: target must be inside the sandbox",
        }
    if not target.exists():
        return {
            "check": "candidate_test",
            "passed": False,
            "detail": (
                f"No file at {filename} — call write_candidate_test() first, "
                "with this exact filename."
            ),
        }

    # Path relative to repo_dir, since _run's cwd is repo_dir — pytest
    # needs the file argument relative to (or resolvable from) that cwd.
    relative_path = target.relative_to(Path(repo_dir).resolve())
    code, out = _run(["pytest", str(relative_path), "-q"], cwd=Path(repo_dir))
    return {
        "check": "candidate_test",
        "passed": code == 0,
        "detail": out or "clean",
    }


if __name__ == "__main__":
    import sys
    repo = str(Path(__file__).parent / "demo_repo")
    result = run_full_gate(repo)
    print("PASSED" if result["passed"] else "FAILED")
    for c in result["checks"]:
        print(f"  [{'OK' if c['passed'] else 'FAIL'}] {c['check']}")
        if not c["passed"]:
            print("    " + c["detail"].replace("\n", "\n    "))
    sys.exit(0 if result["passed"] else 1)
