"""
mcp_server/server.py

Exposes the deterministic gate (gate.py) as an MCP server so the
Reviewer agent can call these tools directly through ADK's McpToolset,
instead of the orchestrator being the only thing that can verify a
claim. This is what makes "the Reviewer found a bug" a checkable fact
rather than an LLM's opinion: the Reviewer can write a failing test
and immediately run it via the same MCP tool the final gate uses.

Run standalone for a smoke test:
    python -m mcp_server.server --smoke-test /path/to/repo

Run as an MCP stdio server (what ADK's McpToolset spawns):
    python -m mcp_server.server
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import core.gate as gate  # noqa: E402

mcp = FastMCP("adversarial-review-gate")


def _ensure_safe_repo_dir(repo_dir: str) -> None:
    """Prevent the LLM from passing arbitrary paths (like /etc) to MCP tools."""
    import tempfile
    from core.path_safety import validate_repo_ref

    repo_path = Path(repo_dir).resolve()
    temp_path = Path(tempfile.gettempdir()).resolve()

    if repo_path.is_relative_to(temp_path) and repo_path.name.startswith("adv_review_sandbox_"):
        return

    try:
        validate_repo_ref(repo_dir)
    except ValueError:
        raise ValueError(f"Access denied: {repo_dir} is neither an allowed repo root nor a valid sandbox.")


@mcp.tool()
def run_linter(repo_dir: str, target_file: str | None = None) -> dict:
    """Run ruff static-analysis lint checks. Pass target_file to scope the
    check to just that file — always correct to do so if you only changed
    one file, since ruff will only ever report on what's actually there."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_linter(repo_dir, target_file)


@mcp.tool()
def run_type_check(repo_dir: str, target_file: str | None = None) -> dict:
    """Run mypy type checking. Pass target_file to scope the check to just
    that file — recommended, since checking the whole repo can fail with
    an unrelated 'Duplicate module named' error on repos containing
    multiple subdirectories with same-named files (e.g. multiple example
    projects each with their own setup.py), which has nothing to do with
    your patch."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_type_check(repo_dir, target_file)


@mcp.tool()
def run_tests(repo_dir: str) -> dict:
    """Run the FULL pytest suite in the given repo directory and report
    pass/fail. Always runs every test, not just ones related to a
    specific file — this is the one check that can catch a patch
    breaking something elsewhere in the repo, so it is intentionally not
    scopeable to a single file."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_tests(repo_dir)


@mcp.tool()
def run_security_scan(repo_dir: str, target_file: str | None = None) -> dict:
    """Run bandit security scanning. Pass target_file to scope the check
    to just that file."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_security_scan(repo_dir, target_file)


@mcp.tool()
def run_full_gate(repo_dir: str, target_file: str | None = None) -> dict:
    """Run the complete deterministic gate: lint + types + tests + security.
    A patch may only be merged if this returns passed=true. target_file,
    when given, scopes lint/type/security to that file only (tests always
    run in full — see run_tests)."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_full_gate(repo_dir, target_file)


@mcp.tool()
def sandbox_copy(repo_dir: str) -> dict:
    """Create an isolated sandbox copy of a repo directory and return its path.
    Use this before writing any candidate patch or test, so nothing touches
    the real working tree until it has passed the gate."""
    _ensure_safe_repo_dir(repo_dir)
    return {"sandbox_path": str(gate.sandbox_copy(repo_dir))}


@mcp.tool()
def write_candidate_test(repo_dir: str, filename: str, content: str) -> dict:
    """Write an executable counterexample test into a sandboxed repo.
    Use this to turn a natural-language critique ('this breaks on empty
    input') into a concrete, runnable pytest test."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.write_candidate_test(repo_dir, filename, content)


@mcp.tool()
def run_candidate_test(repo_dir: str, filename: str) -> dict:
    """Run ONE specific counterexample test you just wrote with
    write_candidate_test, by its exact filename. Use this — not
    run_tests — to confirm your counterexample actually fails. run_tests
    respects whatever test configuration the target repo itself defines
    and can silently skip your file entirely on repos that restrict test
    discovery to a different directory; this tool always runs your exact
    file directly, regardless of that configuration."""
    _ensure_safe_repo_dir(repo_dir)
    return gate.run_candidate_test(repo_dir, filename)


def _smoke_test(repo_dir: str) -> None:
    result = run_full_gate(repo_dir)
    print("PASSED" if result["passed"] else "FAILED")
    for c in result["checks"]:
        print(f"  [{'OK' if c['passed'] else 'FAIL'}] {c['check']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke-test":
        _smoke_test(sys.argv[2])
    else:
        mcp.run(transport="stdio")
