"""Manifest-driven evaluation of repository-context retrieval.

The committed fixtures exercise regression behavior. This module provides the
missing bridge to a user-supplied corpus of real repositories: the manifest
names local checkouts and expected signals, while the evaluator only reads
those checkouts and emits a JSON report. It never downloads repositories or
claims benchmark coverage without an explicit manifest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.repo_context import retrieve_repo_context


def load_corpus_manifest(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a JSON corpus manifest."""
    manifest_path = Path(path)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    repositories = raw.get("repositories") if isinstance(raw, dict) else raw
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("manifest must contain a non-empty repositories list")

    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(repositories, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"repository {index}: expected an object")
        name = entry.get("name")
        repo_path = entry.get("path")
        targets = entry.get("targets")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"repository {index}: name must be a non-empty string")
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ValueError(f"repository {name}: path must be a non-empty string")
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"repository {name}: targets must be a non-empty list")
        validated.append({"name": name, "path": repo_path, "targets": targets})
    return validated


def _target_spec(raw: Any, repository_name: str) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        return {"file": raw}
    if isinstance(raw, dict) and isinstance(raw.get("file"), str) and raw["file"].strip():
        return raw
    raise ValueError(f"repository {repository_name}: each target needs a file path")


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _evaluate_target(repo_root: Path, repository_name: str, raw_target: Any) -> dict[str, Any]:
    target = _target_spec(raw_target, repository_name)
    target_file = target["file"]
    target_path = (repo_root / target_file).resolve()
    result: dict[str, Any] = {
        "file": target_file,
        "passed": False,
        "signals": {},
    }
    if not _within(repo_root, target_path) or not target_path.is_file():
        result["error"] = "target file is missing or outside repository root"
        return result

    try:
        current_code = target_path.read_text(encoding="utf-8")
        context = retrieve_repo_context(
            str(repo_root),
            str(target_path.relative_to(repo_root)),
            current_code,
            history_repo_dir=str(repo_root),
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        result["error"] = str(exc)
        return result

    call_graph = context.get("call_graph", {})
    result["signals"] = {
        "defined_here": call_graph.get("defined_here", []),
        "called_elsewhere": call_graph.get("called_elsewhere", []),
        "callers": call_graph.get("callers", []),
        "prior_fixes": len(context.get("prior_fixes", [])),
        "test_conventions": len(context.get("test_conventions", [])),
    }
    failures: list[str] = []
    expected_callers = target.get("expected_callers", [])
    expected_symbols = target.get("expected_symbols", [])
    if not isinstance(expected_callers, list) or not all(
        isinstance(item, str) for item in expected_callers
    ):
        failures.append("expected_callers must be a list of strings")
    else:
        missing_callers = sorted(set(expected_callers) - set(call_graph.get("callers", [])))
        if missing_callers:
            failures.append(f"missing callers: {', '.join(missing_callers)}")
    if not isinstance(expected_symbols, list) or not all(
        isinstance(item, str) for item in expected_symbols
    ):
        failures.append("expected_symbols must be a list of strings")
    else:
        missing_symbols = sorted(set(expected_symbols) - set(call_graph.get("defined_here", [])))
        if missing_symbols:
            failures.append(f"missing symbols: {', '.join(missing_symbols)}")
    if target.get("require_test_conventions", False) and not context.get("test_conventions"):
        failures.append("no test-convention samples found")

    result["failures"] = failures
    result["passed"] = not failures
    return result


def evaluate_corpus_manifest(path: str | Path) -> dict[str, Any]:
    """Evaluate all targets in a manifest and return a serializable report."""
    repositories = load_corpus_manifest(path)
    report_repositories: list[dict[str, Any]] = []
    total_targets = 0
    passed_targets = 0
    for repository in repositories:
        repo_root = Path(repository["path"]).expanduser().resolve()
        repo_result: dict[str, Any] = {
            "name": repository["name"],
            "path": str(repo_root),
            "targets": [],
        }
        if not repo_root.is_dir():
            repo_result["error"] = "repository path is not a directory"
            repo_result["passed"] = False
            total_targets += len(repository["targets"])
            report_repositories.append(repo_result)
            continue
        for raw_target in repository["targets"]:
            target_result = _evaluate_target(repo_root, repository["name"], raw_target)
            repo_result["targets"].append(target_result)
            total_targets += 1
            if target_result["passed"]:
                passed_targets += 1
        repo_result["passed"] = all(target["passed"] for target in repo_result["targets"])
        report_repositories.append(repo_result)

    return {
        "passed": passed_targets == total_targets and total_targets > 0,
        "repositories": len(report_repositories),
        "targets": total_targets,
        "passed_targets": passed_targets,
        "failed_targets": total_targets - passed_targets,
        "results": report_repositories,
    }


__all__ = ["evaluate_corpus_manifest", "load_corpus_manifest"]
