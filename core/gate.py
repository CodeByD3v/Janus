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


def run_linter(repo_dir: str) -> dict:
    """Run ruff (style + safety lint rules) over the sandboxed repo."""
    code, out = _run(["ruff", "check", "."], cwd=Path(repo_dir))
    return {"check": "linter", "passed": code == 0, "detail": out or "clean"}


def run_type_check(repo_dir: str) -> dict:
    """Run mypy over the sandboxed repo."""
    code, out = _run(
        ["mypy", "--ignore-missing-imports", "."], cwd=Path(repo_dir)
    )
    return {"check": "type_check", "passed": code == 0, "detail": out or "clean"}


def run_tests(repo_dir: str) -> dict:
    """Run the existing (and any newly added) pytest suite."""
    code, out = _run(["pytest", "-q"], cwd=Path(repo_dir))
    return {"check": "tests", "passed": code == 0, "detail": out or "clean"}


def run_security_scan(repo_dir: str) -> dict:
    """Run bandit to catch injection patterns, unsafe deserialization, etc."""
    code, out = _run(
        ["bandit", "-q", "-r", ".", "-x", "./tests"], cwd=Path(repo_dir)
    )
    return {"check": "security_scan", "passed": code == 0, "detail": out or "clean"}


def run_full_gate(repo_dir: str) -> dict:
    """Run all four checks. The patch only merges if every check passes."""
    checks = [
        run_linter(repo_dir),
        run_type_check(repo_dir),
        run_tests(repo_dir),
        run_security_scan(repo_dir),
    ]
    passed = all(c["passed"] for c in checks)
    logger.info(
        "gate_result",
        repo_dir=repo_dir,
        passed=passed,
        checks={c["check"]: c["passed"] for c in checks},
    )
    return {
        "passed": passed,
        "checks": checks,
    }


def write_candidate_test(repo_dir: str, filename: str, content: str) -> dict:
    """Let the Reviewer materialize an executable counterexample as a real
    test file in the sandbox, so a critique becomes a concrete pass/fail
    signal instead of prose."""
    repo_path = Path(repo_dir).resolve()
    target = (repo_path / "tests" / filename).resolve()
    
    if not target.is_relative_to(repo_path):
        return {"error": "Path traversal denied: target must be inside the sandbox"}

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    logger.info("candidate_test_written", target=str(target))
    return {"written": str(target)}


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
