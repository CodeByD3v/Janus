"""validation.py — generic validation runner.

Executes arbitrary shell commands from janus.yaml checks with the same
container isolation and path safety that gate.py already provides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.gate import _run, _validate_sandbox_path  # Reuse gate execution and boundary checks
from core.repo_config import CheckConfig, RepoConfig, load_repo_config


@dataclass
class CheckResult:
    check: str
    passed: bool
    detail: str

def run_check(check: CheckConfig, repo_dir: Path) -> CheckResult:
    """Run a single validation check."""
    # Split the command string into args for subprocess
    cmd = check.command.split()
    returncode, output = _run(cmd, cwd=repo_dir, timeout=check.timeout)
    return CheckResult(
        check=check.name,
        passed=(returncode == 0),
        detail=output or "clean",
    )

def run_checks(
    repo_dir: str,
    config: RepoConfig | None = None,
    target_file: str | None = None,
) -> dict[str, Any]:
    """Run all validation checks. Returns the same contract as run_full_gate."""
    if config is None:
        config = load_repo_config(repo_dir)

    if _validate_sandbox_path(repo_dir) is None:
        return {
            "passed": False,
            "checks": [{
                "check": "sandbox_path",
                "passed": False,
                "detail": "repo_dir is not a validated temporary sandbox path",
            }],
        }

    results = []
    for check_config in config.checks:
        result = run_check(check_config, Path(repo_dir))
        results.append({
            "check": result.check,
            "passed": result.passed,
            "detail": result.detail,
        })

    all_passed = all(r["passed"] for r in results)
    return {"passed": all_passed, "checks": results}
