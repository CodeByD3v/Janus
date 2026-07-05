"""
agents.py — the two ADK agents with asymmetric structure.

DESIGN NOTE: Both agents use the same base Gemini model (controlled by
the ADV_REVIEW_MODEL env var via config.py). The Reviewer's critique
quality comes from retrieval-augmented few-shot examples pulled from a
persistent, growable store of historical "real catch" review comments
(see retrieval.py), NOT from fine-tuned weights.

Fine-tuning the Reviewer on a large, mined dataset of PR comments that
historically preceded a real bug-fix commit is explicit FUTURE WORK.
The retrieval store is the seam where that future dataset plugs in —
for now it starts with a curated seed set and grows via
retrieval_pipeline/ingest.py.

The structural asymmetry IS real and enforced in code, not just prompts:
- Different MCP tool_filters enforce different capabilities
- The Patcher can call run_full_gate; the Reviewer cannot
- The Reviewer can only write test files, not source files
- This is enforced by the MCP server's tool dispatch, not by asking
  the model nicely
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from core.config import settings

_gate_toolset_full = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python3",
            args=[settings.MCP_SERVER_SCRIPT],
        ),
        timeout=120,
    ),
    tool_filter=[
        "sandbox_copy",
        "run_linter",
        "run_type_check",
        "run_tests",
        "run_security_scan",
        "run_full_gate",
    ],
)

_reviewer_toolset = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python3",
            args=[settings.MCP_SERVER_SCRIPT],
        ),
        timeout=120,
    ),
    tool_filter=[
        "sandbox_copy",
        "write_candidate_test",
        "run_tests",
        "run_linter",
        "run_type_check",
        "run_security_scan",
    ],
)

PATCHER_INSTRUCTION = """You are the Patcher agent in an adversarial code
review loop. You are given a ticket describing a bug or feature, and the
current source of the relevant file(s).

Your incentive is to solve the stated problem correctly and efficiently.
You are not trying to write a "perfect" defensive implementation up front —
you converge quickly, then respond to concrete, falsifiable critiques from
the Reviewer agent.

Rules:
- Always propose a complete replacement of the file(s) you are editing, as
  a fenced python code block, never a diff.
- When the Reviewer gives you a critique with a concrete failing test, you
  MUST either (a) fix the code so that test passes, or (b) explain
  specifically why the test's premise is wrong (e.g. it tests behavior
  outside the ticket's scope). Do not silently ignore a failing test.
- Do not push back more than once on the same critique.
- Keep your reasoning short. Optimize for a correct, minimal patch."""

REVIEWER_INSTRUCTION_TEMPLATE = """You are the Reviewer agent in an adversarial code
review loop. You did not write this code and you are not trying to be
helpful in a generic sense — your only incentive is to find real,
concrete defects the Patcher missed, the kind that would actually cause a
bug report or an on-call page.

Below are real historical review comments that preceded an actual bug-fix
commit, retrieved because the flagged pattern resembles the code you are
about to review. Use them as your guide for what a "real catch" looks
like versus a style nit — this retrieval step is standing in for a future
Reviewer fine-tuned on a large, mined dataset of historical PR comments;
for now it draws on a persistent, incrementally-growable store.

Retrieved examples:
{retrieved_examples}

Concretely:

- IGNORE: naming preferences, formatting, comment style, minor
  redundancy, anything ruff/mypy would already catch (those run
  separately — don't repeat them).
- LOOK FOR: edge cases (empty/None/zero/negative inputs), mutation of
  caller-owned state, off-by-one errors, resource leaks, unhandled
  exceptions on realistic inputs, and mismatches between the ticket's
  stated requirement and what the code actually does.
- For every real issue you find, you MUST write an executable
  counterexample using the write_candidate_test tool and run it with
  run_tests to confirm it actually fails against the current code. A
  critique with no failing test attached is not admissible — discard it
  rather than raise vague prose.
- If you find nothing that clears this bar, say so plainly: "No further
  issues found." Do not manufacture a critique to seem thorough.
- You cannot edit the Patcher's source file. You can only sandbox copies,
  write test files, and run checks."""


def build_patcher() -> LlmAgent:
    """Build the Patcher agent with full gate toolset access."""
    return LlmAgent(
        model=settings.MODEL,
        name="patcher",
        description="Proposes and revises code patches for a given ticket.",
        instruction=PATCHER_INSTRUCTION,
        tools=[_gate_toolset_full],
    )


def build_reviewer(retrieved_examples: str = "(none retrieved)") -> LlmAgent:
    """Build the Reviewer agent, injecting retrieved few-shot "real catch"
    examples into its instruction.

    `retrieved_examples` is a pre-formatted string produced by retrieval.py
    — this function does no retrieval itself, it only renders the template.

    NOTE: The Reviewer uses the same base model as the Patcher. Its
    critique quality comes from retrieval-augmented few-shot grounding,
    not from fine-tuned weights. Fine-tuning on a large mined dataset
    is future work — see retrieval_pipeline/ for the ingestion seam.
    """
    instruction = REVIEWER_INSTRUCTION_TEMPLATE.format(
        retrieved_examples=retrieved_examples
    )
    return LlmAgent(
        model=settings.MODEL,
        name="reviewer",
        description=(
            "Critiques a proposed patch using executable counterexamples, "
            "grounded in retrieved historical examples."
        ),
        instruction=instruction,
        tools=[_reviewer_toolset],
    )
