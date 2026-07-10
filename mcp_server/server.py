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


@mcp.tool()
def run_linter(repo_dir: str) -> dict:
    """Run ruff static-analysis lint checks over the given repo directory."""
    return gate.run_linter(repo_dir)


@mcp.tool()
def run_type_check(repo_dir: str) -> dict:
    """Run mypy type checking over the given repo directory."""
    return gate.run_type_check(repo_dir)


@mcp.tool()
def run_tests(repo_dir: str) -> dict:
    """Run the pytest suite in the given repo directory and report pass/fail."""
    return gate.run_tests(repo_dir)


@mcp.tool()
def run_security_scan(repo_dir: str) -> dict:
    """Run bandit security scanning over the given repo directory."""
    return gate.run_security_scan(repo_dir)


@mcp.tool()
def run_full_gate(repo_dir: str) -> dict:
    """Run the complete deterministic gate: lint + types + tests + security.
    A patch may only be merged if this returns passed=true."""
    return gate.run_full_gate(repo_dir)


@mcp.tool()
def sandbox_copy(repo_dir: str) -> dict:
    """Create an isolated sandbox copy of a repo directory and return its path.
    Use this before writing any candidate patch or test, so nothing touches
    the real working tree until it has passed the gate."""
    return {"sandbox_path": str(gate.sandbox_copy(repo_dir))}


@mcp.tool()
def write_candidate_test(repo_dir: str, filename: str, content: str) -> dict:
    """Write an executable counterexample test into a sandboxed repo.
    Use this to turn a natural-language critique ('this breaks on empty
    input') into a concrete, runnable pytest test."""
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
