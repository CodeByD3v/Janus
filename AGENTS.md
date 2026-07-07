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
- Repository-context retrieval — call graph neighbors, prior fix commits,
  test conventions — read fresh from the live repo every round, distinct
  from the behavioral retrieval above (see §6)
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

## 5. Retrieval Contract (Behavioral — "what to review")

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

## 6. Repository-Context Retrieval (Behavioral — "what the repo actually looks like")

The behavioral retrieval in §5 answers "what does a real catch look like" —
it has no idea what's actually in the repo being reviewed. This is a
**separate retrieval concern**, in its own module (`core/repo_context.py`),
with a separate retrieval strategy.

### Why it's separate from `retrieval.py`
Behavioral retrieval is embedding-similarity search over a curated example
set — the query is "what does this code pattern resemble." Repo-context
retrieval is structural — the query is "what else in this specific repo is
relevant to this specific patch." Different retrieval mechanics, different
data source (the live sandboxed repo, not a curated store), different
freshness requirements (re-read every round, so it always reflects the
current patch, not a periodically-ingested batch).

### What it retrieves
- **Call graph neighbors** (`_find_call_graph_neighbors`): AST-based,
  one hop in each direction — which other `.py` files in the repo reference
  a name defined in the file under review, and which names the file under
  review calls that it doesn't define itself. A Reviewer that can't see
  callers can't tell if a signature change breaks something three files away.
- **Prior fix commits** (`_find_prior_fixes`): `git log` on the target file,
  filtered to messages containing a fix-related keyword
  (`fix`, `bug`, `patch`, `issue`, `crash`, `regression`, `hotfix`). A bug
  fixed once and reintroduced is a very high-value catch. Degrades to an
  empty list if the sandbox isn't a git repo — this is expected and handled,
  not an error.
- **Existing test conventions** (`_find_test_conventions`): samples other
  test files in the repo's `tests/` directory, excluding any already
  covering the target file, so Reviewer-written counterexamples match this
  repo's testing style instead of an imported one.

### Where it plugs in
`retrieve_repo_context(repo_dir, target_file, current_code) -> dict` is
called every round in `orchestrator.run_debate`, alongside (not instead of)
`retrieve_examples`. Its output is rendered by
`format_repo_context_for_prompt` into the `{repo_context}` slot in
`REVIEWER_INSTRUCTION_TEMPLATE` — a second, distinct block from
`{retrieved_examples}`, so the two retrieval sources stay legible and
independently debuggable in the rendered instruction. Which signals were
surfaced each round is persisted on `Round.repo_context_signals_json`, the
same way `retrieved_example_ids` is persisted.

### Known limits
- Call graph detection is name-based text scanning, not a real static
  analysis pass — it will produce false positives (a name matching text
  that isn't actually a call) and false negatives (aliased imports,
  dynamic dispatch). Sufficient as a first signal, not a substitute for a
  proper AST-resolved call graph if this becomes a priority later.
- Git history requires the sandbox to be a real git repo with history —
  a plain directory copy (the common case today) yields no prior-fix
  signal, silently. Worth revisiting if git history is desired: sandbox
  creation would need to preserve `.git` rather than being a flat copy.
- Every signal is best-effort and degrades independently — a Reviewer with
  partial repo context should still be better off than one with none, but
  none of this is exhaustive.

---

## 7. Fine-Tuning Interface — the target three-layer architecture

Fine-tuning the Reviewer is real future work, not a hand-wave. The target
architecture looks like:

```
Repository
    │
    ▼
Repo-Context Retrieval (§6 — call graph, git history, test patterns)
    │
    ▼
Behavioral Retrieval (§5 — historical "real catch" examples)
    │
    ▼
Reviewer LLM — fine-tuned on historical high-value review comments,
prompted with both retrieved contexts above
    │
    ▼
Executable counterexample (write_candidate_test + run_tests)
    │
    ▼
Evidence-based critique returned to the Patcher
```

Each layer fixes a different failure mode, and none of them substitutes for
the others:
- **Repo-context retrieval (§6, built)** gives the Reviewer facts about
  *this* codebase it could not otherwise know. Without it, even a perfect
  reviewer is reviewing a file in isolation.
- **Behavioral retrieval (§5, built)** gives the Reviewer a sense of what a
  real catch looks like versus a style nit, without needing fine-tuned
  weights to encode it.
- **Fine-tuning (not yet started)** would give the Reviewer the *skill* of
  reviewing well as a learned prior, rather than as few-shot-prompted
  behavior — cheaper per-call, but expensive to build and, without §5/§6
  alongside it, prone to going stale as languages, frameworks, and repo
  conventions evolve. Pairing it with retrieval is what keeps it current
  without requiring retraining.
- **Execution (built, via the gate)** is what turns any of the above into
  evidence instead of opinion — nothing merges on a critique alone.

Both retrieval layers now exist, but neither is mature: the behavioral
store is a 25-example seed set (§5's known limits) and repo-context
retrieval is name-based text scanning, not a resolved call graph, and
loses git history entirely on a non-git sandbox (§6's known limits).
Fine-tuning stays deferred until growing and hardening both of these is a
worse investment than starting on learned review skill directly — that
point hasn't been reached yet.

To replace the retrieval-augmented Reviewer with a fine-tuned one once that
point is reached:

1. **Training data**: Mine PR review comments from a large set of repos.
   For each comment, check if the lines it points at were touched again
   within ~30 days by a commit whose message suggests a fix. Label
   these as "real catches." Down-weight or discard comments with no
   follow-up fix.

2. **Model requirements**: The fine-tuned Reviewer must:
   - Accept `agents.REVIEWER_INSTRUCTION_TEMPLATE` (or a simplified version
     without the `{retrieved_examples}` slot) — and keep the `{repo_context}`
     slot, since fine-tuning does not remove the need for repo-specific facts
   - Produce output compatible with `write_candidate_test` tool calls
   - Be calibrated to flag issues proportional to historical bug rates,
     not maximum paranoia (false-positive cost is high)
   - Be trained on a deliberately **disjoint** slice of repos/bug types
     from the Patcher's training data to reduce collusion risk

3. **Integration point**: Replace the `build_reviewer()` function in
   `core/agents.py`. The `REVIEWER_INSTRUCTION_TEMPLATE` can be simplified
   (remove `{retrieved_examples}` slot) but should keep the `{repo_context}`
   slot. The rest of the system (orchestrator, gate, API) does not change.

4. **Evaluation**: Use held-out historical PRs where you know the real bug
   that shipped. Does the Reviewer catch it before merge, using only the
   info available at review time (no hindsight leakage)?

---

## 8. Operational Runbook

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
| `GOOGLE_API_KEY` | Worker only | — | Single Gemini API key (fallback if `GOOGLE_API_KEYS` unset) |
| `GOOGLE_API_KEYS` | No | — | Comma-separated pool of Gemini keys (GAP 15) — takes precedence over the singular var if set |
| `GOOGLE_API_KEY_COOLDOWN_SECONDS` | No | `30` | How long a rate-limited key is skipped before the pool retries it |
| `API_KEYS` | API | — | `key1:tenant1,key2:tenant2` — **this service's own tenant auth keys, unrelated to the Google keys above** |
| `DATABASE_URL` | Yes | `sqlite:///./adversarial_code_review.db` | DB connection string |
| `ADV_REVIEW_MODEL` | No | `gemini-2.5-flash` | LLM model name |
| `USE_CONTAINERIZED_GATE` | No | `false` | Enable Docker sandbox |
| `SANDBOX_IMAGE` | If containerized | `adv-review-sandbox:latest` | Sandbox Docker image |

### Scale LLM throughput across multiple Google API keys (GAP 15)
If debate throughput is bottlenecked on a single key's rate limit, set
`GOOGLE_API_KEYS` to a comma-separated list instead of the singular
`GOOGLE_API_KEY`:
```bash
echo "GOOGLE_API_KEYS=key-one,key-two,key-three" >> .env
```
`core/llm_client.py`'s `KeyPool` round-robins across them. What actually
rotates: the Reviewer draws a fresh key every round (it's rebuilt fresh
each round anyway); the Patcher draws one key per debate and only rotates
mid-debate if that key hits a rate limit, since its session persists
across rounds — see `llm_client.py`'s module docstring for the full
reasoning on why this split is safe (every prompt is self-contained, so
rebuilding on rotation loses no state the model needs). Which key index
served each call is visible per-debate in `DebateSession`'s persisted
cost breakdown (`calls_per_key`) — never the raw key.

Ensure keys used this way come from **separate Google Cloud
projects/billing accounts** — keys under the same project typically share
one underlying quota, so pooling keys from a single project doesn't
actually raise the ceiling.

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

## 9. Hard Rules

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
