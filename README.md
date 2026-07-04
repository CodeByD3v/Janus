# Janus
<<<<<<< HEAD

## Adversarial Code Review — Patcher vs. Reviewer Agents

Two ADK agents with genuinely asymmetric incentives debate a code patch;
a deterministic gate (real lint/type/test/security tooling) has the only
vote on whether it merges.

```
Patcher proposes -> Reviewer critiques (with a failing test it wrote and ran)
  -> Patcher fixes or pushes back -> repeat (<=5 rounds) -> deterministic gate
  -> MERGE or REJECT
```

## Why this exists

Single-agent "write the fix" loops solve the stated problem and nothing
else — they have no incentive to look for the bug nobody asked about. A
naive second LLM asked to "review this" tends toward endless nitpicking or
sycophantic approval. This project encodes a structurally different
incentive for the Reviewer instead of just a different prompt:

- The Reviewer **cannot edit source code** — its tools only let it
  sandbox, write *test* files, and run checks.
- Every critique it raises must come with an **executable counterexample**
  it has actually run and confirmed fails against the current code —
  prose-only critiques are discarded by instruction.
- Nothing merges without passing `gate.run_full_gate()` — a plain Python
  function with no model in the loop, independently testable.

## Project layout

```
gate.py                 deterministic gate: ruff, mypy, pytest, bandit via subprocess
mcp_server/server.py    FastMCP stdio server exposing gate.py functions as MCP tools
agents.py               Patcher + Reviewer LlmAgent definitions (asymmetric instructions + tool_filter)
orchestrator.py         the debate loop (InMemoryRunner per agent, capped at 5 rounds)
demo_repo/              seeded demo: inventory.py has two real bugs a weak existing
                         test suite does NOT catch (empty-list crash, mutation bug)
adversarial_code_review.ipynb   Kaggle notebook — the primary deliverable, runs everything above
```

## Setup

```bash
pip install google-adk mcp ruff mypy pytest bandit
export GOOGLE_API_KEY=your_gemini_api_key   # or set as a Kaggle Secret
```

## Run the deterministic gate alone (no API key needed)

```bash
python3 gate.py
```

This runs lint/type/test/security checks against `demo_repo/` and prints
per-check pass/fail. On the unmodified demo repo this **passes**, despite
two real bugs — the existing test suite is deliberately weak. That gap is
exactly what the Reviewer agent exists to close.

## Run the full debate loop (needs `GOOGLE_API_KEY`)

```bash
python3 orchestrator.py
```

Or open `adversarial_code_review.ipynb` — the notebook is self-contained
(it writes all the `.py` files to disk via `%%writefile` cells) and is
the version submitted for judging.

## Architecture

| Course concept | Where it's demonstrated |
|---|---|
| Agent / Multi-agent system (ADK) | `agents.py` — two `LlmAgent`s, each driven by its own `InMemoryRunner` in `orchestrator.py` |
| MCP Server | `mcp_server/server.py` — `FastMCP` stdio server; both agents connect via ADK's `MCPToolset` with different `tool_filter`s |
| Agent skills | Reviewer's instruction encodes a specific, testable procedure (sandbox → write counterexample → run it → only then critique) |
| Security / guardrails | Reviewer's toolset structurally excludes source-write and `run_full_gate`; sandboxing via `gate.sandbox_copy`; merge gated entirely on deterministic checks |
| Deployability | MCP server supports both `StdioConnectionParams` (used here) and `StreamableHTTPConnectionParams` for a remote deployment, with no other code changes needed |

## Security notes

- No API keys are hardcoded anywhere in this repo. The notebook loads
  `GOOGLE_API_KEY` from Kaggle Secrets at runtime.
- All agent-proposed edits happen in an isolated `tempfile.mkdtemp()`
  sandbox — the real working tree is never touched directly.
- The Reviewer's MCP tool filter excludes `run_full_gate` and any tool
  that could write to source files, so it cannot approve or silently
  "fix" its own findings — it can only prove a problem exists.

## Known limitations / future work

- The Reviewer's "hunt for real bugs, ignore style" behavior is currently
  a **prompted** heuristic, not a fine-tuned model. The original design
  called for fine-tuning on PR review comments that historically preceded
  a real bug fix (mined via a 30-day "was this spot touched again by a
  fix commit" heuristic) — out of scope for a 5-day build, tracked as
  future work in the Writeup.
- `MAX_ROUNDS = 5` is a fixed cap, not calibrated against any measured
  false-positive rate.
- Demo scope is a single Python file; no evaluation yet on a corpus of
  held-out historical PRs with known shipped bugs.
=======
Adversarial code review between two LLM agents , a Reviewer grounded in retrieved historical bug examples keeps the Patcher honest, and a strict automated gate has sole merge authority.
>>>>>>> c1ca81723d60b8d3c5785ab25bb22ccf4eb42ff6
