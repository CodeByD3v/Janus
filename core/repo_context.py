"""Repository-context retrieval for the live sandbox under review.

Behavioral retrieval (``core.retrieval``) answers what a historical bug
resembles. This module answers what the current repository actually looks
like: nearby callers, prior fixes, and local test conventions. Every signal
is best-effort and read-only so a partially observable repository never stops
a debate.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Any

from core.config import settings
from core.observability import get_logger
from core.repo_config import load_repo_config

logger = get_logger(__name__)


_SYMBOL_PATTERNS = (
    re.compile(r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:class|interface|trait|struct|enum|type)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"\b(?:def|fn)\s+([A-Za-z_]\w*)"),
    re.compile(r"\b(?:public|private|protected|static|async|final|override|inline|virtual|const|mut)\s+(?:[\w<>\[\],.?]+\s+)+([A-Za-z_]\w*)\s*\("),
)
_CALL_PATTERN = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


def _run_git(args: list[str], cwd: Path, timeout: int | None = None) -> str:
    """Run a git command, returning empty output on any failure."""
    if timeout is None:
        timeout = settings.REPO_CONTEXT_GIT_TIMEOUT
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("git_command_failed", args=args, error=str(exc))
        return ""


def _source_files_for_target(
    repo_dir: Path,
    target_file: str,
    max_files_scanned: int,
) -> list[Path]:
    """Return bounded source files using the target's extension when known."""
    target_path = (repo_dir / target_file).resolve()
    suffix = target_path.suffix.lower()
    candidates: list[Path] = []
    if suffix:
        candidates = sorted(repo_dir.rglob(f"*{suffix}"))
    if not candidates:
        candidates = sorted(repo_dir.rglob("*"))
    return [
        path
        for path in candidates
        if path.is_file()
        and path.resolve() != target_path
        and ".git" not in path.parts
        and not any(part.startswith(".") for part in path.relative_to(repo_dir).parts)
    ][:max_files_scanned]


def _regex_symbols(code: str) -> list[str]:
    names: set[str] = set()
    for pattern in _SYMBOL_PATTERNS:
        names.update(pattern.findall(code))
    return sorted(names)


def _find_call_graph_neighbors(
    repo_dir: Path,
    target_file: str,
    current_code: str,
    max_files_scanned: int | None = None,
) -> dict[str, list[str]]:
    """Find one-hop definitions, calls, and files that reference definitions.

    Python files use the precise AST path where possible. Other languages,
    and Python files with temporarily invalid syntax while being reviewed,
    use conservative regular-expression extraction over files with the same
    extension. This is intentionally a useful signal rather than a compiler-
    grade call graph.
    """
    if max_files_scanned is None:
        max_files_scanned = settings.REPO_CONTEXT_MAX_FILES_SCANNED

    defined_here: set[str] = set()
    called_names: set[str] = set()
    try:
        tree = ast.parse(current_code)
    except (SyntaxError, ValueError, TypeError) as exc:
        logger.info("call_graph_ast_fallback", file=target_file, error=str(exc))
        # Preserve the historical fail-soft behavior for malformed Python:
        # the reviewer should not receive guessed symbols from broken syntax.
        # The regex fallback is intended for recognized non-Python files.
        if Path(target_file).suffix.lower() != ".py":
            defined_here.update(_regex_symbols(current_code))
            called_names.update(_CALL_PATTERN.findall(current_code))
    else:
        defined_here.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        called_names.update(
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        )

    callers: set[str] = set()
    if defined_here:
        for path in _source_files_for_target(repo_dir, target_file, max_files_scanned):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if any(re.search(rf"\b{re.escape(name)}\b", text) for name in defined_here):
                callers.add(str(path.relative_to(repo_dir)))

    return {
        "defined_here": sorted(defined_here),
        "called_elsewhere": sorted(name for name in called_names if name not in defined_here),
        "callers": sorted(callers),
    }


def _find_prior_fixes(
    repo_dir: Path,
    target_file: str,
    max_entries: int | None = None,
) -> list[dict[str, str]]:
    """Return recent fix-like commits touching ``target_file``."""
    if max_entries is None:
        max_entries = settings.REPO_CONTEXT_MAX_PRIOR_FIXES
    keywords = [
        item.strip().lower()
        for item in settings.REPO_CONTEXT_FIX_KEYWORDS.split(",")
        if item.strip()
    ]
    log_output = _run_git(
        ["log", f"-{max_entries * 4}", "--pretty=format:%H|%s", "--", target_file],
        cwd=repo_dir,
    )
    entries: list[dict[str, str]] = []
    for line in log_output.splitlines():
        if "|" not in line:
            continue
        sha, message = line.split("|", 1)
        if any(keyword in message.lower() for keyword in keywords):
            entries.append({"sha": sha[:10], "message": message.strip()})
        if len(entries) >= max_entries:
            break
    return entries


def _test_file_candidates(tests_dir: Path) -> list[Path]:
    """Find common test naming styles for Python and non-Python projects."""
    patterns = ("test_*", "*_test.*", "*.test.*", "*.spec.*", "spec_*")
    found: set[Path] = set()
    for pattern in patterns:
        found.update(path for path in tests_dir.rglob(pattern) if path.is_file())
    return sorted(found)


def _find_test_conventions(
    repo_dir: Path,
    target_file: str,
    max_samples: int | None = None,
    snippet_chars: int | None = None,
    test_patterns: list[str] | None = None,
) -> list[str]:
    """Sample local tests while excluding tests named after the target file."""
    if max_samples is None:
        max_samples = settings.REPO_CONTEXT_MAX_TEST_SAMPLES
    if snippet_chars is None:
        snippet_chars = settings.REPO_CONTEXT_SNIPPET_CHARS
    if test_patterns is None:
        test_patterns = [
            item.strip()
            for item in settings.REPO_CONTEXT_TEST_DIR_NAMES.split(",")
            if item.strip()
        ]

    tests_dir = next(
        (repo_dir / name for name in test_patterns if (repo_dir / name).is_dir()),
        None,
    )
    if tests_dir is None:
        return []

    stem = Path(target_file).stem.lower()
    samples: list[str] = []
    for path in _test_file_candidates(tests_dir):
        if stem and stem in path.stem.lower():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        relative_name = str(path.relative_to(tests_dir))
        samples.append(f"{relative_name}:\n{text[:snippet_chars]}")
        if len(samples) >= max_samples:
            break
    return samples


def retrieve_repo_context(
    repo_dir: str,
    target_file: str,
    current_code: str,
) -> dict[str, Any]:
    """Gather fresh structural facts from the repository under review."""
    repo_path = Path(repo_dir)
    config = load_repo_config(repo_dir)
    call_graph = _find_call_graph_neighbors(repo_path, target_file, current_code)
    prior_fixes = _find_prior_fixes(repo_path, target_file)
    test_conventions = _find_test_conventions(
        repo_path,
        target_file,
        test_patterns=config.test_patterns or None,
    )
    logger.info(
        "retrieve_repo_context",
        target_file=target_file,
        callers=len(call_graph.get("callers", [])),
        prior_fixes=len(prior_fixes),
        test_samples=len(test_conventions),
    )
    return {
        "call_graph": call_graph,
        "prior_fixes": prior_fixes,
        "test_conventions": test_conventions,
    }


def format_repo_context_for_prompt(context: dict[str, Any]) -> str:
    """Render repository-context signals as a distinct prompt block."""
    if not context:
        return "No repository context available."
    parts: list[str] = []
    call_graph = context.get("call_graph", {})
    callers = call_graph.get("callers", [])
    called_elsewhere = call_graph.get("called_elsewhere", [])
    if callers:
        parts.append("Other files referencing functions/classes defined here: " + ", ".join(callers))
    if called_elsewhere:
        parts.append("This file calls names defined elsewhere in the repo: " + ", ".join(called_elsewhere))
    prior_fixes = context.get("prior_fixes", [])
    if prior_fixes:
        parts.append("Prior bug-fix commits touching this file:")
        parts.extend(f"  - {fix['sha']}: {fix['message']}" for fix in prior_fixes)
    test_conventions = context.get("test_conventions", [])
    if test_conventions:
        parts.append("Existing test conventions elsewhere in this repo:")
        parts.extend(f"--- {sample}" for sample in test_conventions)
    return "\n".join(parts) if parts else "No repository context available."
