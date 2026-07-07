"""
agents.py — the two ADK agents with asymmetric structure.

DESIGN NOTE: Both agents use the same base Gemini model (controlled by
the ADV_REVIEW_MODEL env var via config.py). The Reviewer's critique
quality comes from two retrieval sources, not fine-tuned weights:
- Behavioral retrieval (retrieval.py): historical "real catch" review
  comments similar to the code pattern under review.
- Repository-context retrieval (repo_context.py): structural facts about
  THIS specific repo — call graph neighbors, prior fix commits, existing
  test conventions.

Fine-tuning the Reviewer on a large, mined dataset of PR comments that
historically preceded a real bug-fix commit is explicit FUTURE WORK,
and is not worth starting until both retrieval sources above are mature
— see AGENTS.md's Fine-Tuning Interface section for the full picture.

The structural asymmetry IS real and enforced in code, not just prompts:
- Different MCP tool_filters enforce different capabilities
- The Patcher can call run_full_gate; the Reviewer cannot
- The Reviewer can only write test files, not source files
- This is enforced by the MCP server's tool dispatch, not by asking
  the model nicely

KEY POOLING (GAP 15): both build_patcher() and build_reviewer() draw a
model instance bound to one key from core.llm_client's KeyPool, instead
of a plain model-name string. This spreads load across multiple Google
API keys instead of hitting one key's rate limit as the sole ceiling.
Each function returns (agent, key_index) so the caller can report a 429
back to the pool via llm_client.get_key_pool().mark_rate_limited(index)
without ever handling the raw key.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from core.config import settings
from core.llm_client import build_model

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

Below is structural context about the repository itself — other places in
this repo that call into or are called by the code you're reviewing, prior
commits that fixed bugs in this same file, and how tests elsewhere in this
repo are conventionally written. Use it to catch issues invisible from the
patch alone, such as a signature change that breaks a caller elsewhere, or
a bug being silently reintroduced after it was already fixed once.

Repository context:
{repo_context}

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


def build_patcher() -> tuple[LlmAgent, int]:
    """Build the Patcher agent with full gate toolset access.

    Returns (agent, key_index) — key_index identifies which pool key this
    agent is bound to, so a 429 can be reported back to the pool via
    `llm_client.get_key_pool().mark_rate_limited(key_index)`.
    """
    model, key_index = build_model(settings.MODEL)
    agent = LlmAgent(
        model=model,
        name="patcher",
        description="Proposes and revises code patches for a given ticket.",
        instruction=PATCHER_INSTRUCTION,
        tools=[_gate_toolset_full],
    )
    return agent, key_index


def build_reviewer(
    retrieved_examples: str = "(none retrieved)",
    repo_context: str = "No repository context available.",
) -> tuple[LlmAgent, int]:
    """Build the Reviewer agent, injecting two distinct retrieved contexts
    into its instruction: behavioral examples (retrieval.py) and
    repository-structure facts (repo_context.py).

    Both text arguments are pre-formatted strings — this function does no
    retrieval itself, it only renders the template. Neither retrieval
    source requires fine-tuned weights; fine-tuning remains future work
    — see AGENTS.md's Fine-Tuning Interface section.

    Returns (agent, key_index) — since this is called fresh every round
    (see orchestrator.run_debate), the Reviewer gets a freshly-drawn key
    from the pool every round, unlike the Patcher which is built once per
    debate. See llm_client.py's module docstring for why.
    """
    instruction = REVIEWER_INSTRUCTION_TEMPLATE.format(
        retrieved_examples=retrieved_examples,
        repo_context=repo_context,
    )
    model, key_index = build_model(settings.MODEL)
    agent = LlmAgent(
        model=model,
        name="reviewer",
        description=(
            "Critiques a proposed patch using executable counterexamples, "
            "grounded in retrieved historical examples and repository context."
        ),
        instruction=instruction,
        tools=[_reviewer_toolset],
    )
    return agent, key_index
