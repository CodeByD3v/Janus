# Claude Implementation Prompt — Adversarial Code Review, Production Build

Paste this entire file as your first message to Claude. It contains full project context,
all current code, every known gap, and precise instructions for what to build.

---

## What this project is

**Adversarial Code Review: Infrastructure for Patcher vs. Reviewer Agents** — a
production service, not a demo script. It runs adversarial code-review debates
(a Patcher agent proposes fixes, a Reviewer agent critiques them with executable
counterexamples, a deterministic gate has sole merge authority) as a multi-tenant
API that other systems can call, with the isolation, persistence, and operational
guarantees that implies.

### The honest framing (Option A — this is the design we're committed to)

The project is NOT claiming to have fine-tuned two models with different incentives.
It IS claiming to have built:

- Structural role asymmetry enforced via MCP tool filters (not just prompts)
- A Reviewer that can only prove bugs exist via executable counterexamples — it cannot fix code
- A deterministic gate (real ruff/mypy/pytest/bandit runs) that is the ONLY thing
  with merge authority — not the LLMs — executed inside locked-down, resource-capped
  containers so an untrusted patch can never touch the host
- A **RAG-augmented Reviewer** grounded in a retrieval store of historical "real
  catch" review comments, retrieved per-round and injected as few-shot examples
- A retrieval pipeline that can be grown over time (batch ingestion of newly mined
  examples) without redeploying the service
- Production infrastructure around all of the above: persistence, an authenticated
  API, observability, concurrency, and CI/CD

Fine-tuning a Reviewer on a large mined PR dataset remains explicit **future
work** — the retrieval store starts curated and is expected to grow, but nothing
here claims fine-tuned weights exist. Every claim in this project must be
accurate; the code comments and README must say so, not hide it.

---

## Repository layout (what exists, what needs to be created)

```
adversarial_code_review/
├── AGENTS.md                         ← MISSING — must create
├── README.md                         ← exists, needs rewrite for prod deployment
├── requirements.txt                  ← MISSING — must create
├── pyproject.toml                    ← MISSING — must create (packaging + tool config)
├── Dockerfile                        ← MISSING — must create (service image)
├── docker/
│   └── sandbox.Dockerfile            ← MISSING — must create (locked-down gate-execution image)
├── config.py                         ← MISSING — must create (env-driven settings, no hardcoded secrets)
├── gate.py                           ← exists, check logic unchanged, execution path hardened (see gaps)
├── agents.py                         ← exists, needs REVIEWER_INSTRUCTION + comment rework for RAG
├── retrieval.py                      ← MISSING — must create (persistent vector store + retrieval)
├── retrieval_pipeline/
│   ├── __init__.py                   ← MISSING — must create
│   ├── ingest.py                     ← MISSING — must create (batch ingestion of new examples)
│   └── schema.py                     ← MISSING — must create (example record schema + validation)
├── data/
│   └── real_catch_examples.seed.jsonl ← MISSING — must create (seed set, loaded on first boot)
├── orchestrator.py                   ← exists, needs hardening + retrieval step + persistence
├── storage/
│   ├── __init__.py                   ← MISSING — must create
│   ├── models.py                     ← MISSING — must create (DebateSession, Round, ORM models)
│   └── db.py                         ← MISSING — must create (connection/session management, migrations)
├── api/
│   ├── __init__.py                   ← MISSING — must create
│   ├── app.py                        ← MISSING — must create (FastAPI app)
│   ├── auth.py                       ← MISSING — must create (API key auth + per-key rate limiting)
│   └── schemas.py                    ← MISSING — must create (request/response pydantic models)
├── worker.py                         ← MISSING — must create (queue consumer running debates async)
├── observability.py                  ← MISSING — must create (structured logging, metrics, cost tracking)
├── mcp_server/
│   ├── __init__.py                   ← exists
│   └── server.py                     ← exists, complete
├── demo_repo/
│   ├── inventory.py                  ← exists, intentionally buggy (kept as the reference fixture)
│   ├── pytest.ini                    ← exists
│   └── tests/
│       └── test_inventory.py         ← exists, intentionally weak
├── evals/
│   ├── __init__.py                   ← MISSING — must create
│   ├── eval_gate.py                  ← MISSING — must create
│   ├── eval_retrieval.py             ← MISSING — must create
│   ├── eval_reviewer.py              ← MISSING — must create
│   └── eval_api.py                   ← MISSING — must create (auth, rate limit, request validation)
├── .github/
│   └── workflows/
│       ├── ci.yml                    ← MISSING — must create (lint, type check, unit evals on every PR)
│       └── deploy.yml                ← MISSING — must create (build + push image on main)
└── adversarial_code_review.ipynb     ← exists, kept only as an exploratory notebook, not the entry point
```

---

## Current code (read every file before writing anything)

### gate.py (check LOGIC unchanged — execution path must be hardened, see GAP 1)
```python
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
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
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


def sandbox_copy(repo_dir: str) -> Path:
    """Copy the repo into an isolated temp dir so agent-proposed edits
    never touch the real working tree until they pass the gate."""
    tmp = Path(tempfile.mkdtemp(prefix="adv_review_sandbox_"))
    shutil.copytree(repo_dir, tmp, dirs_exist_ok=True)
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
    return {
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
    }


def write_candidate_test(repo_dir: str, filename: str, content: str) -> dict:
    """Let the Reviewer materialize an executable counterexample as a real
    test file in the sandbox, so a critique becomes a concrete pass/fail
    signal instead of prose."""
    target = Path(repo_dir) / "tests" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
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
```

**Why this can't ship as-is:** `_run` calls `subprocess.run` directly against
whatever host the service happens to be running on, with whatever permissions
that host process has, and no CPU/memory ceiling beyond a wall-clock timeout.
An adversarial or simply broken Patcher patch executes as part of `pytest -q`
and `bandit` — arbitrary code execution on the service host is one crafted
patch away. GAP 1 fixes this without touching the four check functions'
signatures or return contracts.

### agents.py (REVIEWER_INSTRUCTION and docstring need rework — see TASKS below)
```python
"""
agents.py — the two ADK agents with asymmetric structure.

CURRENT PROBLEM WITH THIS FILE: comments say things like "the Patcher and
Reviewer have different incentives" and "the Reviewer is fine-tuned on..."
which are NOT TRUE. Both use the same base Gemini model. The Reviewer's
critique quality comes from retrieval-augmented few-shot examples pulled
from a persistent, growable store of historical "real catch" review
comments, not from fine-tuned weights. Fine-tuning on a larger, mined
dataset is future work. All such claims must be reworded — see TASKS below.

The structural asymmetry IS real:
- Different MCP tool_filters enforce different capabilities in code
- The Reviewer structurally cannot write to source files or call run_full_gate
- This is enforced by the MCP server, not by asking the model nicely
"""

from __future__ import annotations

import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

MODEL = os.getenv("ADV_REVIEW_MODEL", "gemini-2.5-flash")
SERVER_SCRIPT = str(Path(__file__).resolve().parent / "mcp_server" / "server.py")

_gate_toolset_full = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="python3",
            args=[SERVER_SCRIPT],
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
            args=[SERVER_SCRIPT],
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
    return LlmAgent(
        model=MODEL,
        name="patcher",
        description="Proposes and revises code patches for a given ticket.",
        instruction=PATCHER_INSTRUCTION,
        tools=[_gate_toolset_full],
    )


def build_reviewer(retrieved_examples: str = "(none retrieved)") -> LlmAgent:
    """Build the Reviewer agent, injecting retrieved few-shot "real catch"
    examples into its instruction. `retrieved_examples` is a pre-formatted
    string produced by retrieval.py — this function does no retrieval
    itself, it only renders the template."""
    instruction = REVIEWER_INSTRUCTION_TEMPLATE.format(
        retrieved_examples=retrieved_examples
    )
    return LlmAgent(
        model=MODEL,
        name="reviewer",
        description="Critiques a proposed patch using executable counterexamples, grounded in retrieved historical examples.",
        instruction=instruction,
        tools=[_reviewer_toolset],
    )
```

### orchestrator.py (needs hardening, retrieval, persistence, and retry — see TASKS below)
```python
"""
orchestrator.py — the debate loop mechanics.

In production this is called by worker.py (a queue consumer), not run
directly as a script. `run_debate` must be safe to call concurrently
across many (repo, ticket) pairs.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types

from agents import build_patcher, build_reviewer
from gate import run_full_gate, sandbox_copy
from retrieval import retrieve_examples, format_examples_for_prompt

MAX_ROUNDS = 5
APP_NAME = "adversarial_code_review"

CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class RoundLog:
    round_num: int
    patch_text: str
    reviewer_text: str
    gate_result: dict
    retrieved_example_ids: list[str] = field(default_factory=list)
    stop_reason: str | None = None


@dataclass
class DebateResult:
    merged: bool
    rounds: list[RoundLog] = field(default_factory=list)
    final_gate: dict | None = None
    sandbox_path: str | None = None


async def _ask(runner: InMemoryRunner, session_id: str, user_id: str, text: str) -> str:
    message = genai_types.Content(role="user", parts=[genai_types.Part(text=text)])
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text
    return final_text


def _extract_code(text: str, fallback: str) -> str:
    match = CODE_BLOCK_RE.search(text)
    return match.group(1) if match else fallback


async def run_debate(repo_dir: str, target_file: str, ticket: str) -> DebateResult:
    sandbox = sandbox_copy(repo_dir)
    target_path = sandbox / target_file
    current_code = target_path.read_text()

    patcher_agent = build_patcher()
    patcher_runner = InMemoryRunner(agent=patcher_agent, app_name=APP_NAME)

    user_id = "service_account"
    patcher_session = str(uuid.uuid4())
    await patcher_runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=patcher_session
    )

    result = DebateResult(merged=False, sandbox_path=str(sandbox))

    patch_prompt = (
        f"Ticket:\n{ticket}\n\n"
        f"Current contents of {target_file}:\n```python\n{current_code}\n```\n\n"
        f"Propose your patch as a full replacement file."
    )
    patch_text = await _ask(patcher_runner, patcher_session, user_id, patch_prompt)
    current_code = _extract_code(patch_text, current_code)
    target_path.write_text(current_code)

    for round_num in range(1, MAX_ROUNDS + 1):
        examples = retrieve_examples(current_code, top_k=3)
        reviewer_agent = build_reviewer(format_examples_for_prompt(examples))
        reviewer_runner = InMemoryRunner(agent=reviewer_agent, app_name=APP_NAME)
        reviewer_session = str(uuid.uuid4())
        await reviewer_runner.session_service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=reviewer_session
        )

        review_prompt = (
            f"Ticket:\n{ticket}\n\n"
            f"Patcher's current version of {target_file} "
            f"(sandbox at {sandbox}):\n```python\n{current_code}\n```\n\n"
            f"The repo root for your tools is: {sandbox}\n"
            f"Review this patch. If you find a real issue, write an "
            f"executable counterexample test and run it to confirm it "
            f"fails, then report the failure. If nothing clears the bar, "
            f"say 'No further issues found.'"
        )
        reviewer_text = await _ask(
            reviewer_runner, reviewer_session, user_id, review_prompt
        )

        gate_result = run_full_gate(str(sandbox))

        stop_reason = None
        if "no further issues found" in reviewer_text.lower():
            stop_reason = "reviewer_satisfied"
        elif round_num == MAX_ROUNDS:
            stop_reason = "max_rounds_reached"

        result.rounds.append(
            RoundLog(
                round_num=round_num,
                patch_text=patch_text,
                reviewer_text=reviewer_text,
                gate_result=gate_result,
                retrieved_example_ids=[ex["id"] for ex in examples],
                stop_reason=stop_reason,
            )
        )

        if stop_reason:
            break

        fix_prompt = (
            f"Reviewer's critique:\n{reviewer_text}\n\n"
            f"Current contents of {target_file}:\n```python\n{current_code}\n```\n\n"
            f"Fix the issue if it's real, or explain briefly why you're "
            f"pushing back (only once per critique). Propose your patch as "
            f"a full replacement file."
        )
        patch_text = await _ask(patcher_runner, patcher_session, user_id, fix_prompt)
        current_code = _extract_code(patch_text, current_code)
        target_path.write_text(current_code)

    final_gate = run_full_gate(str(sandbox))
    result.final_gate = final_gate
    result.merged = final_gate["passed"]
    return result


def print_debate_summary(result: DebateResult) -> None:
    print(f"Sandbox: {result.sandbox_path}")
    for r in result.rounds:
        print(f"\n--- Round {r.round_num} ---")
        print("Reviewer:", r.reviewer_text[:400])
        print("Gate at this round:", "PASS" if r.gate_result["passed"] else "FAIL")
        if r.stop_reason:
            print("Stop reason:", r.stop_reason)
    print("\n=== FINAL GATE ===")
    for c in result.final_gate["checks"]:
        print(f"  [{'OK' if c['passed'] else 'FAIL'}] {c['check']}")
    print("MERGED" if result.merged else "REJECTED — did not pass the gate")


if __name__ == "__main__":
    ticket = (
        "average_price() should return the average unit price of the given "
        "items (0.0 for an empty list). apply_bulk_discount() should give a "
        "10% discount when total quantity across items is >= 50, and must "
        "not mutate the caller's input list/objects — return a new list."
    )
    demo_repo = str(Path(__file__).parent / "demo_repo")
    outcome = asyncio.run(run_debate(demo_repo, "inventory.py", ticket))
    print_debate_summary(outcome)
```

---

## Known gaps — what is broken or missing (fix ALL of these)

### GAP 1 — gate.py executes untrusted code directly on the host
`_run` shells out to `ruff`/`mypy`/`pytest`/`bandit` in the same process tree as
the service, with no CPU/memory/PID limits and no network isolation. A patch that
imports something malicious, forks, or busy-loops runs with the service's own
permissions.

Fix, without changing the four check functions' signatures or return contracts:
- Build `docker/sandbox.Dockerfile`: a minimal image containing only Python,
  ruff, mypy, pytest, bandit, and their pinned versions — no service code, no
  credentials, no network access at runtime.
- Change `_run` (or add a `_run_containerized` used internally by the four check
  functions) to execute each command inside a fresh container mounting the
  sandboxed repo dir read-write, with: `--network none`, a memory limit (e.g.
  `--memory 512m`), a CPU limit (e.g. `--cpus 1`), a PID limit
  (`--pids-limit 128`), and `--read-only` on everything except the mounted repo
  dir and `/tmp`. Keep the existing wall-clock timeout as a second layer, not a
  replacement for the container limits.
- `run_full_gate` and the individual `run_*` functions keep the exact same
  return shape (`{"check": ..., "passed": ..., "detail": ...}`) — callers
  (agents, evals, the API) must not need to change because of this fix.
- If Docker is unavailable in the eval/CI environment, `eval_gate.py` must skip
  the containerized-execution tests (mark them, don't fail the suite) while
  still running the pure-logic tests against a local subprocess fallback.

### GAP 2 — no persistence; every debate is stateless and disappears on restart
There is currently no database. A crashed process loses every in-flight debate,
and there's no audit trail of past reviews, retrieved examples, or gate results.

Fix:
- Add `storage/models.py` with ORM models (SQLAlchemy is fine) for
  `DebateSession` (id, repo_ref, target_file, ticket, status, created_at,
  updated_at, merged, final_gate_json) and `Round` (id, session_id, round_num,
  patch_text, reviewer_text, gate_result_json, retrieved_example_ids_json,
  stop_reason, created_at).
- Add `storage/db.py`: engine/session factory driven by a `DATABASE_URL` env
  var (default to local SQLite for dev, must support Postgres via the same
  code path for prod), plus a `run_migrations()` entrypoint (Alembic is fine,
  or a minimal hand-rolled migration runner if you want zero extra deps beyond
  what's pinned).
- `orchestrator.run_debate` persists a `DebateSession` row at start, and a
  `Round` row after every round (not just at the end) so an in-flight debate is
  recoverable/inspectable even if the process dies mid-debate.

### GAP 3 — no API layer; the only entrypoint is a local script
`orchestrator.py`'s `if __name__ == "__main__"` block is the only way to trigger
a debate. Production needs a real interface other systems can call.

Fix — build `api/app.py` (FastAPI):
- `POST /debates` — body: `{repo_ref, target_file, ticket}` → enqueues a debate,
  returns `{debate_id, status: "queued"}` immediately (does not block on the
  full multi-round debate inline).
- `GET /debates/{debate_id}` — returns current status, rounds so far, and final
  gate result once available, read from the `storage` layer.
- `GET /healthz` — liveness/readiness check (DB reachable, sandbox container
  image present).
- All endpoints require auth (see GAP 4) and validate request bodies with
  pydantic models in `api/schemas.py`.

### GAP 4 — no auth, no rate limiting, no multi-tenancy
Anyone who can reach the process can trigger arbitrarily many debates (each of
which costs LLM tokens and container time).

Fix — build `api/auth.py`:
- API-key auth: a header-based key, hashed at rest, mapped to a tenant/caller id.
  No plaintext keys in code, config files, or logs — pull from the configured
  secrets source via `config.py`, never hardcode.
- Per-key rate limiting (a simple token bucket keyed by API key is sufficient;
  store bucket state in the same DB or an in-memory store if single-instance,
  but make the limiter pluggable so it can move to Redis when the service is
  scaled horizontally).
- Every `DebateSession` row records which tenant/API key created it.

### GAP 5 — orchestrator.py _extract_code is silent on failure
If the Patcher returns prose with no code block, `_extract_code` silently returns
the old code as the "new" patch. The Patcher then thinks it shipped a fix when it
didn't. Fix: if no code block is found, log a warning AND add a `code_extraction_failed`
field to RoundLog (and the persisted `Round` row).

### GAP 6 — orchestrator.py has no retry or circuit breaking on transient API errors
If `_ask()` raises any exception (network blip, rate limit, ADK timeout), the
entire debate crashes with no recovery, and a systemically failing dependency
(e.g. the model API is down) will retry-storm it on every request.
Fix:
- Wrap `_ask()` in a retry with exponential backoff, max 3 attempts, catching
  broad `Exception`. Log each retry attempt at WARNING via `observability.py`.
- Add a simple circuit breaker in front of `_ask()` (e.g. open after N
  consecutive failures within a rolling window, reject fast with a clear error
  while open, half-open after a cooldown) so a persistent outage fails fast
  instead of holding worker capacity on doomed retries.
- Use only stdlib for backoff (`asyncio.sleep`); the circuit breaker can be a
  small hand-rolled class — no new dependency needed for either.

### GAP 7 — orchestrator.py does not detect prose-only Reviewer responses
If the Reviewer writes a critique paragraph but never calls `write_candidate_test`,
the orchestrator cannot currently tell. Fix: after receiving reviewer_text, check
whether `write_candidate_test` was actually invoked by inspecting whether any new
file appeared in `sandbox/tests/` that wasn't there before. If the Reviewer gave
a non-empty critique but wrote no test, add a `reviewer_skipped_counterexample: True`
flag to RoundLog (and the persisted `Round` row). This is a logging/observability
fix, not a hard stop.

### GAP 8 — retrieval store is not persistent or growable
There is currently no `retrieval.py` and no way to add new "real catch" examples
without editing source. Build:
- `data/real_catch_examples.seed.jsonl`: 20-30 hand-curated examples used to
  bootstrap the store on first boot. Each line a JSON object with `id`,
  `bug_pattern` (short tag, e.g. "mutates_caller_list", "off_by_one",
  "unhandled_none", "resource_leak"), `code_snippet`, `review_comment`, and
  `fix_summary`. Validate every record against `retrieval_pipeline/schema.py`
  on ingestion — reject malformed records rather than silently skipping them.
- `retrieval.py`: backed by a **persistent** vector store (a local ChromaDB
  persistent client pointed at a mounted volume, or pgvector if you're already
  running Postgres for GAP 2 — pick one and be consistent) — not an in-memory
  structure rebuilt from a flat file on every process start. Exposes
  `retrieve_examples(current_code: str, top_k: int = 3) -> list[dict]` and
  `format_examples_for_prompt(examples: list[dict]) -> str`.
- `retrieval_pipeline/ingest.py`: a batch job (invoked as
  `python -m retrieval_pipeline.ingest path/to/new_examples.jsonl`) that
  validates and embeds new examples and upserts them into the persistent store
  without downtime or a service restart. This is the seam future mining
  (scraping GitHub PR comments correlated with fix commits) plugs into.
- Retrieval must be deterministic and must not require a network call to an
  external API at query time (embedding at ingest time is fine to call an
  embedding API if needed, but query-time retrieval for every review round
  must not add an avoidable external network dependency and single point of
  failure to every round of every debate).

### GAP 9 — no observability
There is no structured logging, no metrics, and no cost tracking anywhere.
Fix — build `observability.py`:
- Structured (JSON) logging, one logger configured at module level and reused
  everywhere — replace all `print()` calls in orchestrator.py.
- Counters/histograms for: debates started/completed/merged/rejected, rounds
  per debate, gate check pass/fail by check type, retry counts, circuit
  breaker state transitions, and reviewer_skipped_counterexample occurrences.
  Expose them in a Prometheus-scrapeable format via a `/metrics` endpoint on
  the API, or push to whatever metrics backend `config.py` points at.
- Token/cost tracking per LLM call (`_ask`), aggregated per debate and per
  tenant, persisted alongside the `DebateSession` row so cost is queryable
  after the fact.

### GAP 10 — no concurrency model; only one debate can run at a time
`run_debate` is directly callable but there is no mechanism for running many
debates concurrently across tenants without them stepping on each other.
Fix — build `worker.py`:
- A queue consumer (any of: a simple polling loop against a `queued` status in
  the DB, or a real queue like Redis/RQ/Celery if you want proper backpressure
  — polling the DB is acceptable for the first production cut as long as it's
  documented as the thing to swap for a real queue under load) that picks up
  queued `DebateSession` rows, runs `run_debate`, and writes results back.
- Multiple worker processes must be safely runnable in parallel without double
  -processing the same session (use a DB-level claim/lock, e.g. an atomic
  `UPDATE ... SET status='running' WHERE status='queued' AND id=... RETURNING`).
- Each worker gets its own sandbox directory and its own containerized gate
  execution — no shared mutable state between concurrent debates.

### GAP 11 — no CI/CD
Fix — build `.github/workflows/ci.yml` (lint, type check, `eval_gate.py`,
`eval_retrieval.py`, `eval_api.py` on every PR; `eval_reviewer.py` only if a
`GOOGLE_API_KEY` secret is present, otherwise skipped) and
`.github/workflows/deploy.yml` (build the service image and the sandbox image
from `Dockerfile` / `docker/sandbox.Dockerfile`, push on merge to main, run
`storage.db.run_migrations()` as a pre-deploy step).

### GAP 12 — secrets and config are not centralized
Model names, DB URLs, API keys, and container resource limits must not be
scattered as inline defaults across files. Fix: `config.py` reads everything
from environment variables (with sane non-secret defaults only, e.g.
`MAX_ROUNDS`), validates required secrets are present at startup and fails
fast with a clear error if not, and is the single import point every other
module uses for configuration — no module reads `os.getenv` directly except
`config.py` itself.

### GAP 13 — README.md needs reframing for a production system
The existing README reads like a project pitch, not operational documentation.
There is no separate writeup or design-doc deliverable — README.md is the one
document for this repo, and it must describe a production service: what's
deployed, how it's operated, what its guarantees are, and what's still
explicitly future work (fine-tuning the Reviewer on a large mined dataset).
See TASKS below.

### GAP 14 — Reviewer has no repository context, only the one file it's handed
The existing retrieval store (`retrieval.py`, GAP 8) answers "what does a real
catch look like" via similarity search over curated example comments — it is
entirely blind to the actual repo being reviewed. The Reviewer currently sees
only the target file's contents; it has no visibility into callers, prior
fixes to the same lines, or how similar code is already tested elsewhere in
the repo. This is a distinct retrieval concern from GAP 8 (different query,
different data source — the live repo, not a curated store — different
freshness requirements) and must be its own module, not folded into
`retrieval.py`. See TASK 15.

This also means the "fine-tuning is future work" framing (GAP 1's honest
framing, agents.py's docstring, AGENTS.md's Fine-Tuning Interface section)
should be read as a three-layer target architecture, not fine-tuning vs.
retrieval: repo-context retrieval (GAP 14, not yet built) + behavioral
retrieval (GAP 8, built) + a fine-tuned Reviewer (not yet built, and not
worth starting until both retrieval layers are mature) + the deterministic
gate's execution (built). Each layer fixes a different failure mode; none
substitutes for the others.

### GAP 15 — Single API key means one rate limit for the whole service
`config.py` currently reads one `GOOGLE_API_KEY`. Every Patcher and Reviewer
call across every concurrent debate, across every tenant, draws from the same
underlying quota. Under real multi-tenant load this becomes the throughput
ceiling regardless of how many workers are running.

Fix: a key pool, not a single key.
- `config.py` reads `GOOGLE_API_KEYS` (comma-separated) instead of a single
  `GOOGLE_API_KEY` — keep the singular var working as a one-key fallback so
  existing deployments don't break.
- A small `KeyPool` (new `llm_client.py`, or a class inside `config.py`) that
  hands out keys round-robin per outgoing call, marks a key "cooling down"
  for N seconds on a 429/rate-limit error, and skips cooling-down keys when
  picking the next one.
- Wire this into the existing retry loop around `_ask()` in orchestrator.py
  (GAP 6) — on a rate-limit error, the retry should try the next key in the
  pool before backing off on the same one.
- Simplest alternative, if pooling feels like overkill to start: assign the
  Patcher and Reviewer distinct keys statically (they're separate roles
  already), which halves load per key with no pooling logic at all. Note in
  AGENTS.md which approach was taken and why.
- Document, per-key, in `observability.py`: which key (by index, never the
  raw key) served which call, so usage/cost skew across keys is visible.

### GAP 16 — deploy.yml builds and migrates, but deploys nowhere
The existing `.github/workflows/deploy.yml` builds and pushes the service and
sandbox images to a registry, and runs DB migrations — and stops there.
Nothing pulls the new image onto a running host or restarts the service.
"Push to main" today does not result in the live system actually changing.

Fix — pick one deploy target and complete the pipeline. Given the worker
spawns sibling containers for the gate (GAP 1), a serverless target (Cloud
Run, Fargate without privileged mode) actively fights this pattern; prefer:
- **Simplest**: add a step to `deploy.yml` that SSHes into a known host and
  runs `docker compose pull && docker compose up -d`, matching the
  docker-compose topology that already exists.
- **More scalable**: Kubernetes, with the api/worker/sandbox topology mapped
  from `docker-compose.yml` almost directly (api Deployment, worker
  Deployment with a privileged/Docker-in-Docker sidecar or host socket mount,
  Postgres as a managed service instead of in-cluster). Document the
  privileged-pod tradeoff explicitly if this route is taken.
Whichever is chosen, `deploy.yml` must end with the new image actually
serving traffic, not just sitting in a registry.

### GAP 17 — No visibility into a debate's outcome for the people whose code it reviewed
Today the only way to see a debate's result is to already know its
`debate_id` and call `GET /debates/{id}` directly. There is no integration
with the place a developer would naturally look — the pull request itself.

Fix — after `run_debate` completes (in `worker.py`, alongside the existing
DB write), post the outcome where the relevant human already is:
- **GitHub PR comment or Check Run**: the Reviewer's critique history and
  final gate result, posted directly on the PR that triggered the debate —
  this requires the initiating request to carry a PR/commit reference
  through `DebateSession`, which `api/schemas.py`'s request model does not
  currently support and should be extended to accept.
- **Webhook/Slack notification** as a lighter-weight alternative or addition:
  a summary + link to `GET /debates/{id}` posted to a configured webhook URL
  on merge or reject.
Neither requires a new UI — both are additional side effects at the end of
an already-completed debate.

---

## Precise tasks — do these in order

### TASK 1: Harden gate.py's execution path (GAP 1)
Build `docker/sandbox.Dockerfile` and containerize execution of the four check
functions as described. Keep function signatures and return shapes identical.

### TASK 2: Add persistence (GAP 2)
Build `storage/models.py`, `storage/db.py`, and wire `orchestrator.run_debate`
to persist a `DebateSession` row at start and a `Round` row after every round.

### TASK 3: Build the retrieval store and pipeline (GAP 8)
Build `retrieval.py`, `retrieval_pipeline/schema.py`,
`retrieval_pipeline/ingest.py`, and `data/real_catch_examples.seed.jsonl`.
Verify the seed set loads into the persistent store on first boot and survives
a process restart.

### TASK 4: Rework agents.py for retrieval-augmented generation
Rewrite the module docstring and all inline comments so no claim about
"different incentives" or "fine-tuning" is made without the word "future work."
Replace `REVIEWER_INSTRUCTION` with `REVIEWER_INSTRUCTION_TEMPLATE` (a
`.format()`-able string with a `{retrieved_examples}` slot) and change
`build_reviewer()` to accept a `retrieved_examples: str` argument that it
renders into the template.

### TASK 5: Harden orchestrator.py (GAP 5, GAP 6, GAP 7)
Wire in the retrieval step from TASK 3/4, add retry + circuit breaking around
`_ask`, detect silent code-extraction failures and skipped counterexamples, and
replace all `print()` calls with `observability.py`'s logger. Add
`code_extraction_failed`, `reviewer_skipped_counterexample`, and
`retrieved_example_ids` to both `RoundLog` and the persisted `Round` row.

### TASK 6: Build the API layer (GAP 3, GAP 4)
Build `api/app.py`, `api/auth.py`, `api/schemas.py`. `POST /debates` enqueues
(does not run inline); `GET /debates/{id}` reads from `storage`; `GET /healthz`
checks DB and sandbox-image availability; every endpoint requires a valid,
rate-limited API key.

### TASK 7: Build the worker (GAP 10)
Build `worker.py`: a queue consumer that safely claims queued
`DebateSession` rows, runs `run_debate`, writes results back, and can be run
as multiple parallel processes without double-processing a session.

### TASK 8: Build observability (GAP 9)
Build `observability.py`: structured logging setup, metrics counters/histograms,
a `/metrics` endpoint on the API, and per-call token/cost tracking persisted
alongside `DebateSession`.

### TASK 9: Centralize config (GAP 12)
Build `config.py`. Audit every other file for inline `os.getenv` calls,
hardcoded model names, hardcoded resource limits, or hardcoded connection
strings, and route them through `config.py` instead. Fail fast at startup if a
required secret is missing.

### TASK 10: Write the eval suite
- `evals/eval_gate.py`: pure-logic tests always run; containerized-execution
  tests run only if Docker is available, otherwise skipped (not failed).
- `evals/eval_retrieval.py`: retrieval ranking correctness, persistence across
  a simulated restart, and `retrieval_pipeline.ingest` upsert behavior.
- `evals/eval_reviewer.py`: integration test, `pytest.mark.integration`, needs
  `GOOGLE_API_KEY`, asserts a full debate merges within `MAX_ROUNDS`.
- `evals/eval_api.py`: auth rejection on missing/invalid keys, rate-limit
  enforcement, request validation errors, and `GET /debates/{id}` returning
  persisted state correctly.

### TASK 11: Write CI/CD (GAP 11)
Build `.github/workflows/ci.yml` and `.github/workflows/deploy.yml` as
described in GAP 11.

### TASK 12: Write Dockerfile, docker-compose (or equivalent), and requirements.txt
`Dockerfile` for the service (API + worker share the same image, different
entrypoints). Pin all dependencies in `requirements.txt` with a Python version
comment. If a local multi-container dev setup is useful, add a
`docker-compose.yml` wiring the API, worker, DB, and sandbox image together —
this is optional but recommended so a new engineer can run the whole stack
with one command.

### TASK 13: Write AGENTS.md
Format: plain markdown. Sections:
1. Project overview
2. Agent roles table (Patcher / Reviewer — role, model, tools available, cannot do)
3. MCP server contract
4. Gate contract, including the container isolation guarantees from GAP 1
5. Retrieval contract (behavioral): what's in the store, how it's grown via
   `retrieval_pipeline/ingest.py`, and its known limits
6. Repository-context retrieval (planned): what it retrieves (call graph,
   git history, test patterns), why it's a separate module from behavioral
   retrieval, and its integration point (a `{repo_context}` prompt slot)
7. Fine-tuning interface: the target three-layer architecture (repo-context
   retrieval + behavioral retrieval + fine-tuned weights + execution), why
   fine-tuning is deferred until both retrieval layers are mature, and what
   a fine-tuned Reviewer would need to satisfy to replace the
   retrieval-augmented one
8. Operational runbook: how to deploy, how to scale workers, how to rotate API
   keys, what to check first when a debate is stuck
9. Hard rules (things the agent must never do)

### TASK 14: Rewrite README.md (GAP 13)
- Add a "What this is — and what it isn't" section up front: structural
  contribution is real, retrieval grounding is real and growable, fine-tuning
  a Reviewer on a large mined dataset is future work.
- Add deployment instructions (how to run the full stack, required env vars,
  how to seed the retrieval store, how to rotate secrets) and a "Design
  honesty" section mirroring AGENTS.md.
- No separate writeup or design-doc file — README.md is the single source of
  truth for anyone reading the repo.

### TASK 15: Build repo_context.py — repository-context retrieval (GAP 14)
This is a separate retrieval concern from the existing behavioral retrieval
in `retrieval.py` (GAP 8) — different query, different data source, different
freshness requirements. Do not merge it into `retrieval.py`.

Build `repo_context.py` exposing
`retrieve_repo_context(repo_dir: str, target_file: str, current_code: str) -> dict`,
returning at minimum:
- **Call graph neighbors**: functions/classes calling, or called by, the
  code under review (a simple AST-based traversal of the repo is sufficient
  to start — no need for a full static-analysis toolchain up front).
- **Git history**: prior commits touching the same lines, flagged if the
  commit message suggests a bug fix (`git log -L` or `git blame` plus a
  keyword heuristic on commit messages is sufficient to start).
- **Existing test patterns**: how similar functions elsewhere in the repo
  are already tested, so Reviewer-written counterexamples match repo
  convention.

Wire its output into a new `{repo_context}` slot in
`REVIEWER_INSTRUCTION_TEMPLATE`, alongside — not replacing — the existing
`{retrieved_examples}` slot, so the two retrieval sources stay legible and
independently debuggable in the rendered instruction. Update
`orchestrator.run_debate` to call `retrieve_repo_context` each round
alongside `retrieve_examples`, and persist which repo-context signals were
surfaced per round the same way `retrieved_example_ids` is persisted today.

This is real, buildable now, and improves review quality independent of
whether fine-tuning ever happens — build this before revisiting fine-tuning.

### TASK 16: Add a multi-key pool for LLM calls (GAP 15)
Change `config.py` to read `GOOGLE_API_KEYS` (plural, comma-separated) with
`GOOGLE_API_KEY` (singular) kept working as a one-key fallback. Build a
`KeyPool` that round-robins across keys and cools down any key that just hit
a rate limit. Wire it into the existing retry logic around `_ask()` in
orchestrator.py so a 429 tries the next key before backing off. Log which key
index served each call in `observability.py`. If pooling is more than this
warrants right now, the minimum acceptable version is: Patcher and Reviewer
each get their own static key — note in AGENTS.md whichever approach was
taken.

### TASK 17: Complete the deploy pipeline (GAP 16)
Extend `.github/workflows/deploy.yml` so it doesn't stop at "image pushed."
Add the step that actually rolls the new image out — SSH + `docker compose
pull && up -d` for the simplest version, or a Kubernetes apply step if that's
the chosen target. Document the choice and, if Kubernetes, the privileged-pod
tradeoff for the worker's sandbox-spawning requirement, in README.md.

### TASK 18: Post debate outcomes where the developer already is (GAP 17)
Extend `api/schemas.py`'s request model to optionally accept a PR/commit
reference alongside `repo_ref`/`target_file`/`ticket`. After `run_debate`
completes in `worker.py`, if a PR reference was provided, post the critique
history and final gate result as a PR comment or Check Run. Otherwise (or in
addition), support a configured webhook URL for a merge/reject summary. Keep
both as optional side effects — a debate with no PR reference and no webhook
configured behaves exactly as it does today.

---

## Constraints — read these before writing any code

1. gate.py's four check functions (`run_linter`, `run_type_check`, `run_tests`,
   `run_security_scan`) keep their exact signatures and return shapes — only
   their execution path may change (GAP 1).
2. Do NOT change mcp_server/server.py. It is tested and correct.
3. Do NOT change demo_repo/inventory.py or demo_repo/tests/test_inventory.py.
   They remain the reference fixture used by eval_reviewer.py.
4. All new Python must pass `ruff check` and `mypy --ignore-missing-imports`.
5. No hardcoded secrets, API keys, or connection strings anywhere, in any file,
   including test fixtures and Docker images. Everything flows through
   `config.py` and environment variables / a secrets manager.
6. Every dependency must be pinned in `requirements.txt`; no ad hoc `pip install`
   in Dockerfiles outside of what's pinned.
7. Every module that talks to the LLM API, the DB, or the sandbox container
   must handle and log its own failure modes — no bare `except: pass`.
8. AGENTS.md must be written so a new engineer who has never seen this project
   could both operate the deployed service and implement a fine-tuned Reviewer
   that slots in to replace the retrieval-augmented one.

---

## ADK version notes (important — the API has sharp edges)

- `MCPToolset` is from `google.adk.tools.mcp_tool`
- `StdioConnectionParams` is from `google.adk.tools.mcp_tool.mcp_session_manager`
- `StdioServerParameters` is from `mcp` (not from google.adk)
- `InMemoryRunner` is from `google.adk.runners` — note that despite the name,
  in this production build session *state* is what's persisted via
  `storage/models.py`; `InMemoryRunner` is still fine to use as the ADK runner
  itself, since durability comes from the DB layer around it, not from the
  runner.
- `LlmAgent` is from `google.adk.agents`
- Session creation: `await runner.session_service.create_session(app_name=..., user_id=..., session_id=...)`
- Running: `async for event in runner.run_async(user_id=..., session_id=..., new_message=...)`
- Model string: `"gemini-2.5-flash"` (controlled by `ADV_REVIEW_MODEL` env var,
  read via `config.py`)
- These import paths worked against google-adk as installed via pip on Python 3.12 in July 2026.
  If any import fails, check the installed package structure before assuming the path is wrong.

---

## What success looks like

```bash
# Local dev stack (API + worker + DB + sandbox image)
docker compose up --build

# Should pass with no API key, no Docker needed for the pure-logic subset
pytest evals/eval_gate.py -v
pytest evals/eval_retrieval.py -v
pytest evals/eval_api.py -v

# Should pass with Docker available (containerized gate execution)
pytest evals/eval_gate.py -v -m "not skip_no_docker"

# Should pass with GOOGLE_API_KEY set
pytest evals/eval_reviewer.py -v -m integration

# End to end: enqueue a debate via the API, poll until merged
curl -X POST localhost:8000/debates -H "X-API-Key: $KEY" \
  -d '{"repo_ref": "demo_repo", "target_file": "inventory.py", "ticket": "..."}'
curl localhost:8000/debates/{id} -H "X-API-Key: $KEY"
```

And `ruff check .` and `mypy --ignore-missing-imports .` should both pass clean
across the whole project directory. `ci.yml` should be green on a fresh clone
with no manual setup beyond secrets.
