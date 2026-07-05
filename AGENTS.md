# AGENTS.md — Adversarial Code Review System

This document is the authoritative reference for anyone operating,
extending, or replacing components of the adversarial code review system.

---

## 1. Project Overview

This system runs adversarial code-review debates between two LLM agents
(Patcher and Reviewer) with a deterministic verification gate holding sole
merge authority. It is deployed as a multi-tenant API backed by persistent
storage, a queue-based worker, and observable infrastructure.

**What is real:**
- Structural role asymmetry enforced via MCP tool filters (not just prompts)
- A Reviewer that can only prove bugs exist via executable counterexamples
- A deterministic gate (ruff/mypy/pytest/bandit) executed in resource-capped
  containers — the ONLY thing with merge authority
- RAG-augmented Reviewer grounded in a retrieval store of historical "real
  catch" review comments, retrieved per-round as few-shot examples
- A retrieval pipeline that can be grown (batch ingestion) without redeployment
- Production infrastructure: persistence, authenticated API, observability,
  concurrency, CI/CD

**What is future work:**
- Fine-tuning the Reviewer on a large mined dataset of PR comments that
  historically preceded a real bug-fix commit

---

## 2. Agent Roles

| Property | Patcher | Reviewer |
|---|---|---|
| **Role** | Proposes and revises code patches for a given ticket | Finds concrete defects the Patcher missed |
| **Model** | Gemini (controlled by `ADV_REVIEW_MODEL` env var) | Same base model as Patcher |
| **Critique quality source** | N/A — solves the ticket | Retrieval-augmented few-shot examples from historical "real catch" store |
| **MCP tools available** | `sandbox_copy`, `run_linter`, `run_type_check`, `run_tests`, `run_security_scan`, `run_full_gate` | `sandbox_copy`, `write_candidate_test`, `run_tests`, `run_linter`, `run_type_check`, `run_security_scan` |
| **Cannot do** | — | Cannot call `run_full_gate`, cannot write to source files (only test files) |
| **Structural enforcement** | MCP `tool_filter` on `MCPToolset` | MCP `tool_filter` on `MCPToolset` |

The asymmetry is **structural** — the MCP server's tool dispatch enforces it,
not the prompt. The Reviewer literally cannot exceed its role regardless of
how it interprets instructions.

---

## 3. MCP Server Contract

**Location:** `mcp_server/server.py` — DO NOT MODIFY.

The MCP server exposes `core/gate.py` functions as tools over stdio (FastMCP):

| Tool | Signature | Returns |
|---|---|---|
| `run_linter` | `(repo_dir: str)` | `{"check": "linter", "passed": bool, "detail": str}` |
| `run_type_check` | `(repo_dir: str)` | `{"check": "type_check", "passed": bool, "detail": str}` |
| `run_tests` | `(repo_dir: str)` | `{"check": "tests", "passed": bool, "detail": str}` |
| `run_security_scan` | `(repo_dir: str)` | `{"check": "security_scan", "passed": bool, "detail": str}` |
| `run_full_gate` | `(repo_dir: str)` | `{"passed": bool, "checks": [check_results...]}` |
| `sandbox_copy` | `(repo_dir: str)` | `{"sandbox_path": str}` |
| `write_candidate_test` | `(repo_dir: str, filename: str, content: str)` | `{"written": str}` |

ADK agents connect via `MCPToolset` with `StdioConnectionParams`. Each agent's
`tool_filter` restricts which tools it can call.

---

## 4. Gate Contract

The deterministic gate is the **sole merge authority**. No LLM output can
override it.

### Checks (run in order)
1. **Linter** — `ruff check .`
2. **Type checker** — `mypy --ignore-missing-imports .`
3. **Tests** — `pytest -q` (includes any tests the Reviewer wrote)
4. **Security scan** — `bandit -q -r . -x ./tests`

### Return contract
```python
{"passed": bool, "checks": [{"check": str, "passed": bool, "detail": str}, ...]}
```

### Container isolation (when `USE_CONTAINERIZED_GATE=true`)
Each gate command executes inside a fresh Docker container with:
- `--network none` — no network access
- `--memory 512m` — memory ceiling
- `--cpus 1` — CPU limit
- `--pids-limit 128` — process limit
- `--read-only` — filesystem read-only except mounted repo dir and `/tmp`
- Wall-clock timeout as a second layer

The sandbox image (`docker/sandbox.Dockerfile`) contains ONLY Python, ruff,
mypy, pytest, and bandit — no service code, no credentials.

### Fallback
When Docker is unavailable (dev/CI), `core/gate.py` falls back to direct
subprocess execution with only the wall-clock timeout.

---

## 5. Retrieval Contract

### What's in the store
A ChromaDB persistent vector store containing "real catch" review comment
examples — historical review comments that preceded an actual bug-fix commit.

Each record has:
- `id` — unique identifier
- `bug_pattern` — short tag (e.g., `mutates_caller_list`, `off_by_one`)
- `code_snippet` — the code that had the bug
- `review_comment` — the review comment that flagged it
- `fix_summary` — what the fix did

### How it's seeded
On first boot, `retrieval.initialize_store()` loads
`data/real_catch_examples.seed.jsonl` (25 curated examples) into ChromaDB.
Subsequent boots are no-ops if the collection already has data.

### How it's grown
```bash
python -m retrieval_pipeline.ingest path/to/new_examples.jsonl
```
This validates each record against the Pydantic schema, embeds it locally
via sentence-transformers, and upserts into ChromaDB. Safe to run while the
service is live. Duplicate IDs are upserted, not duplicated.

### Known limits
- Seed set is 25 examples — small. Quality depends on growing this store.
- Embeddings are computed locally (all-MiniLM-L6-v2) — fast but not the
  highest-quality embeddings available.
- No active learning loop: the store grows only by manual batch ingestion,
  not automatically from production debates (yet).

---

## 6. Fine-Tuning Interface

The retrieval store is explicitly a **stand-in** for a future fine-tuned
Reviewer model. To replace the retrieval-augmented Reviewer with a
fine-tuned one:

1. **Training data**: Mine PR review comments from a large set of repos.
   For each comment, check if the lines it points at were touched again
   within ~30 days by a commit whose message suggests a fix. Label
   these as "real catches." Down-weight or discard comments with no
   follow-up fix.

2. **Model requirements**: The fine-tuned Reviewer must:
   - Accept `agents.REVIEWER_INSTRUCTION_TEMPLATE` (or a simplified version
     without the `{retrieved_examples}` slot)
   - Produce output compatible with `write_candidate_test` tool calls
   - Be calibrated to flag issues proportional to historical bug rates,
     not maximum paranoia (false-positive cost is high)
   - Be trained on a deliberately **disjoint** slice of repos/bug types
     from the Patcher's training data to reduce collusion risk

3. **Integration point**: Replace the `build_reviewer()` function in
   `core/agents.py`. The `REVIEWER_INSTRUCTION_TEMPLATE` can be simplified
   (remove `{retrieved_examples}` slot). The rest of the system
   (orchestrator, gate, API) does not change.

4. **Evaluation**: Use held-out historical PRs where you know the real bug
   that shipped. Does the Reviewer catch it before merge, using only the
   info available at review time (no hindsight leakage)?

---

## 7. Operational Runbook

### Deploy the full stack
```bash
# Set secrets in .env
echo "GOOGLE_API_KEY=your-key-here" > .env
echo "API_KEYS=your-api-key:your-tenant-id" >> .env

# Build sandbox image first
docker compose --profile build build sandbox-builder

# Start everything
docker compose up --build
```

### Required environment variables
| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_API_KEY` | Worker only | — | Gemini API key for LLM calls |
| `API_KEYS` | API | — | `key1:tenant1,key2:tenant2` format |
| `DATABASE_URL` | Yes | `sqlite:///./adversarial_code_review.db` | DB connection string |
| `ADV_REVIEW_MODEL` | No | `gemini-2.5-flash` | LLM model name |
| `USE_CONTAINERIZED_GATE` | No | `false` | Enable Docker sandbox |
| `SANDBOX_IMAGE` | If containerized | `adv-review-sandbox:latest` | Sandbox Docker image |

### Scale workers
Run additional worker processes — each polls the DB independently and claims
sessions atomically via `claim_queued_session()`:
```bash
docker compose up --scale worker=4
```

### Rotate API keys
1. Add new key to `API_KEYS` env var: `oldkey:tenant,newkey:tenant`
2. Restart the API process (rolling restart is safe)
3. Migrate callers to the new key
4. Remove old key from `API_KEYS`

### When a debate is stuck
1. Check `GET /debates/{id}` — is status `running`, `error`, or `queued`?
2. If `error`: read `error_message` field for the exception
3. If `running` for too long: check worker logs for circuit breaker opens,
   LLM retry warnings, or gate timeout messages
4. Check `GET /healthz` — is the DB reachable? Is the sandbox image present?
5. Check `GET /metrics` — look at `acr_circuit_breaker_opens_total` and
   `acr_llm_retries_total` for sustained API issues

---

## 8. Hard Rules

These are non-negotiable constraints enforced in code:

1. **The gate decides.** No LLM output can override `run_full_gate()`. A patch
   merges if and only if `final_gate["passed"] == True`.

2. **The Reviewer cannot write source files.** Its MCP `tool_filter` excludes
   any tool that writes to source files. It can only write test files via
   `write_candidate_test`.

3. **The Reviewer cannot call `run_full_gate`.** It cannot approve a merge.

4. **No hardcoded secrets.** All secrets flow through `core/config.py` and
   environment variables. API keys are hashed at rest. No plaintext keys
   in code, config files, or logs.

5. **Untrusted code runs in containers.** When `USE_CONTAINERIZED_GATE=true`,
   all gate commands run in network-isolated, resource-capped containers.

6. **Every round is persisted immediately.** If a worker crashes mid-debate,
   all completed rounds are recoverable from the database.

7. **Claims are atomic.** `claim_queued_session()` uses DB-level locking to
   prevent double-processing across parallel workers.

8. **No fine-tuning claims.** The codebase and all documentation must not
   claim fine-tuned model weights exist. The Reviewer's quality comes from
   retrieval-augmented few-shot grounding. Fine-tuning is future work.
