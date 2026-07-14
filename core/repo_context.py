"""
repo_context.py — repository-context retrieval (GAP 14 / TASK 15).

This is a SEPARATE retrieval concern from retrieval.py's behavioral
retrieval, not a variant of it:

- retrieval.py answers "what does a real catch look like" via embedding
  similarity search over a curated store of historical review comments.
  It has no idea what's actually in the repo being reviewed.
- repo_context.py answers "what does THIS repo actually look like" —
  call graph neighbors, prior fix commits touching the same lines, and
  existing test conventions — read directly from the live sandboxed repo
  at review time, so it's always fresh as of the current commit rather
  than a periodically-ingested batch.

Every signal here is best-effort and read-only. If any individual signal
can't be gathered (unparseable code, no git history because the sandbox
copy isn't a git repo, no tests directory) it degrades to an empty result
for that signal rather than failing the whole call — a Reviewer with
partial repo context is still better off than one with none.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from typing import Any

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)


def _run_git(args: list[str], cwd: Path, timeout: int | None = None) -> str:
    """Run a git command, returning empty string on any failure.

    A sandbox copy made via shutil.copytree is not a git repo, so this
    is expected to no-op quietly whenever git history isn't available —
    that's a normal, handled case, not an error.
    """
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
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("git_command_failed", args=args, error=str(e))
        return ""


def _find_call_graph_neighbors(
    repo_dir: Path,
    target_file: str,
    current_code: str,
    max_files_scanned: int | None = None,
) -> dict[str, list[str]]:
    """Best-effort AST-based call graph, one hop in each direction.

    Returns:
        defined_here: top-level functions/classes defined in target_file
        called_elsewhere: names this file calls that it doesn't define itself
        callers: other .py files in the repo that reference a name defined
            in target_file (a Reviewer that can't see these can't tell if a
            signature change breaks something three files away)
    """
    if max_files_scanned is None:
        max_files_scanned = settings.REPO_CONTEXT_MAX_FILES_SCANNED

    try:
        tree = ast.parse(current_code)
    except SyntaxError as e:
        logger.warning("call_graph_parse_failed", file=target_file, error=str(e))
        return {"defined_here": [], "called_elsewhere": [], "callers": []}

    defined_here = sorted(
        {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
    )
    called_names = sorted(
        {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
    )

    callers: set[str] = set()
    if defined_here:
        target_name = Path(target_file).name
        py_files = [
            p for p in repo_dir.rglob("*.py") if p.name != target_name and ".git" not in p.parts
        ][:max_files_scanned]

        for path in py_files:
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if any(name in text for name in defined_here):
                callers.add(str(path.relative_to(repo_dir)))

    return {
        "defined_here": defined_here,
        "called_elsewhere": [n for n in called_names if n not in defined_here],
        "callers": sorted(callers),
    }


def _find_prior_fixes(
    repo_dir: Path,
    target_file: str,
    max_entries: int | None = None,
) -> list[dict[str, str]]:
    """Prior commits touching target_file whose message suggests a bug fix.

    A bug fixed once and reintroduced is a very high-value catch — this
    surfaces that history to the Reviewer instead of leaving it invisible.
    """
    if max_entries is None:
        max_entries = settings.REPO_CONTEXT_MAX_PRIOR_FIXES
    fix_keywords = [
        kw.strip() for kw in settings.REPO_CONTEXT_FIX_KEYWORDS.split(",") if kw.strip()
    ]

    log_output = _run_git(
        ["log", f"-{max_entries * 4}", "--pretty=format:%H|%s", "--", target_file],
        cwd=repo_dir,
    )
    if not log_output:
        return []

    entries: list[dict[str, str]] = []
    for line in log_output.splitlines():
        if "|" not in line:
            continue
        sha, message = line.split("|", 1)
        if any(kw in message.lower() for kw in fix_keywords):
            entries.append({"sha": sha[:10], "message": message.strip()})
        if len(entries) >= max_entries:
            break
    return entries


def _find_test_conventions(
    repo_dir: Path,
    target_file: str,
    max_samples: int | None = None,
    snippet_chars: int | None = None,
) -> list[str]:
    """Sample existing test files elsewhere in the repo, excluding any
    already covering target_file, so Reviewer-written counterexamples
    match this repo's testing conventions instead of an imported style.

    Checks settings.REPO_CONTEXT_TEST_DIR_NAMES in order (default:
    "tests", "testing", "test") and uses the first that exists.
    Previously hardcoded to "tests" only, which silently returned zero
    samples on any repo using a different convention — confirmed
    concretely against a real external repo, pytest-dev/pluggy, which
    uses "testing" and returned zero samples before this fix, despite
    having 9 real test files.

    Unrelated to, and does not change, gate.py's write_candidate_test/
    run_candidate_test, which deliberately keep a fixed, predictable
    "tests/" write location regardless of the repo's own convention —
    that's a different concern (a location run_candidate_test can always
    find by exact path) from this function's concern (sampling existing
    style to inform what the Reviewer writes).
    """
    if max_samples is None:
        max_samples = settings.REPO_CONTEXT_MAX_TEST_SAMPLES
    if snippet_chars is None:
        snippet_chars = settings.REPO_CONTEXT_SNIPPET_CHARS

    dir_names = [
        n.strip()
        for n in settings.REPO_CONTEXT_TEST_DIR_NAMES.split(",")
        if n.strip()
    ]
    tests_dir = next(
        (repo_dir / name for name in dir_names if (repo_dir / name).exists()),
        None,
    )
    if tests_dir is None:
        return []

    stem = Path(target_file).stem
    samples: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if stem in path.name:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        samples.append(f"{path.name}:\n{text[:snippet_chars]}")
        if len(samples) >= max_samples:
            break
    return samples


def retrieve_repo_context(
    repo_dir: str,
    target_file: str,
    current_code: str,
) -> dict[str, Any]:
    """Gather structural facts about the repo being reviewed.

    Returns a dict with `call_graph`, `prior_fixes`, and `test_conventions`
    keys. Safe to call every round — it re-reads the live sandbox each
    time, so results always reflect the current patch, not a stale cache.
    """
    repo_path = Path(repo_dir)

    call_graph = _find_call_graph_neighbors(repo_path, target_file, current_code)
    prior_fixes = _find_prior_fixes(repo_path, target_file)
    test_conventions = _find_test_conventions(repo_path, target_file)

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
    """Render repo-context signals as text for the Reviewer's prompt.

    Kept as a distinct block from retrieval.py's
    `format_examples_for_prompt` output, so the two retrieval sources
    stay legible and independently debuggable in the rendered
    instruction rather than being merged into one undifferentiated blob.
    """
    if not context:
        return "No repository context available."

    parts: list[str] = []

    call_graph = context.get("call_graph", {})
    callers = call_graph.get("callers", [])
    called_elsewhere = call_graph.get("called_elsewhere", [])
    if callers:
        parts.append(
            "Other files referencing functions/classes defined here: " + ", ".join(callers)
        )
    if called_elsewhere:
        parts.append(
            "This file calls names defined elsewhere in the repo: " + ", ".join(called_elsewhere)
        )

    prior_fixes = context.get("prior_fixes", [])
    if prior_fixes:
        parts.append("Prior bug-fix commits touching this file:")
        for fix in prior_fixes:
            parts.append(f"  - {fix['sha']}: {fix['message']}")

    test_conventions = context.get("test_conventions", [])
    if test_conventions:
        parts.append("Existing test conventions elsewhere in this repo:")
        for sample in test_conventions:
            parts.append(f"--- {sample}")

    if not parts:
        return "No repository context available."

    return "\n".join(parts)
