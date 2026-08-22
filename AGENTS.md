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
- A Reviewer whose `ISSUE_FOUND` verdict is accepted only after an exact newly
  written counterexample executes and reports a failure; otherwise the verdict
  is normalized to `INCONCLUSIVE`
- Language-agnostic validation via `janus.yaml`
- Reviewer-first debate loop (Patcher is reactive)
- Multi-provider BYOK support (Gemini, Claude, GPT, etc.)
- A deterministic gate executed in resource-capped
  containers — the ONLY thing with merge authority
- RAG-augmented Reviewer grounded in a retrieval store of historical "real
  catch" review comments, retrieved per-round as few-shot examples
- Repository-context retrieval — call graph neighbors, prior fix commits,
  test conventions — read fresh from the live repo every round, distinct
  from the behavioral retrieval above (see §6)
- A retrieval pipeline that can be grown (batch ingestion) without redeployment
- Production infrastructure: persistence, authenticated API, observability,
  concurrency, CI/CD
- Configurable, fail-closed debate controls with Prometheus calibration metrics
  for verdicts, evidence acceptance/rejection, repository-context signals, and
  max-round termination
- Curated Python/TypeScript/Go regression fixtures and offline-rendered Kubernetes
  manifests with an ordered migration release script
- Manifest-driven repository-context evaluation for operator-supplied local
  repositories, plus non-Python comment/string masking before regex fallback
- Read-only calibration reports from Prometheus snapshots and an explicitly
  confirmed GitHub webhook smoke-test harness; neither changes thresholds nor
  contacts external services by default
- Async lifecycle hardening: blocking persistence, gate, sandbox, and worker
  DB operations are offloaded; LLM deadlines and bounded MCP teardown mitigate
  the observed event-loop stall, whose third-party root cause remains unconfirmed
- Fail-closed admin visibility tier with cross-tenant `GET /admin/debates`
  summaries and `/admin` operator dashboard; tenant/admin roles are disjoint
- GitHub App webhook handler (`/janus review` comment trigger, PR
  open/sync events, fork PR protection, HMAC-SHA256 signature
  verification) — see `api/github_app.py`
- Enterprise auto-merge with configurable branch patterns and trusted
  author lists, gated by `janus.yaml` opt-in and INCONCLUSIVE verdict
  blocking — see `core/auto_merge.py`

**What remains open or future work:**
- The root cause of the observed async event-loop stall after a failing LLM/MCP
  sequence is not confirmed. Runtime mitigation is implemented through
  `asyncio.to_thread`, bounded persistence timeouts, LLM deadlines, and bounded
  shielded MCP teardown. `scripts/reproduce_persist_hang.sh` now also records
  independent process state and can optionally capture periodic py-spy dumps;
  definitive diagnosis still requires a persistent terminal and ptrace access.
- Live GitHub App end-to-end validation remains pending: it requires a real
  App registration, installation, public webhook endpoint, and test
  repository. `evals/eval_github_app.py` verifies signatures, trigger parsing,
  and file filtering with mocked GitHub calls; `scripts/smoke_github_app.py`
  provides a manually confirmed signed-payload smoke test but is not run here.
- **Fine-tuning the Reviewer remains unstarted as a model-training operation.**
  `training/dataset.py` and `scripts/prepare_reviewer_dataset.py` provide a
  provider-neutral boundary that requires repository/PR/review/commit provenance
  and executable before/after evidence. The 25-example retrieval seed is
  intentionally rejected by that boundary; no training job or trained weights
  are claimed. Revisit after a substantially larger audited corpus exists and
  retrieval/context signals are validated across more repositories.
- The Kubernetes bundle is render-tested offline but has not been validated
  against a live cluster, storage class, node pool, or public ingress.
- Calibration is now reportable from observed telemetry through
  `scripts/reviewer_calibration_report.py`, but thresholds remain operator-set
  and are not auto-tuned without representative production or replay data.
- The corpus evaluator accepts an explicit manifest of local real repositories,
  but no external corpus is bundled or claimed as benchmark coverage.

---

## 2. Agent Roles

| Property | Patcher | Reviewer |
|---|---|---|
| **Role** | Proposes and revises code patches for a given ticket (**RESPONDER**, only runs if `ISSUE_FOUND`) | Finds concrete defects the Patcher missed |
| **Model** | Gemini (controlled by `ADV_REVIEW_MODEL` env var) | Same base model as Patcher |
| **Critique quality source** | N/A — solves the ticket | Retrieval-augmented few-shot examples from historical "real catch" store |
| **MCP tools available** | `sandbox_copy`, `run_linter`, `run_type_check`, `run_tests`, `run_security_scan`, `run_full_gate` | `sandbox_copy`, `write_candidate_test`, `run_tests`, `run_linter`, `run_type_check`, `run_security_scan` |
| **Cannot do** | — | Cannot call `run_full_gate`, cannot write to source files (only test files) |
| **Structural enforcement** | MCP `tool_filter` on `MCPToolset` | MCP `tool_filter` on `MCPToolset` |

The Reviewer outputs one of three verdicts: `PASS`, `ISSUE_FOUND`, or `INCONCLUSIVE`.
An `ISSUE_FOUND` verdict must be backed by an exact candidate test that pytest
actually executes and reports as failing; missing, passing, skipped, or
collection-error evidence is fail-closed as `INCONCLUSIVE` with human review.

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
These are now **CONFIGURABLE DEFAULTS**. If `janus.yaml` exists in the repository, its checks override these defaults:
1. **Linter** — `ruff check .`
2. **Type checker** — `mypy --ignore-missing-imports .`
3. **Tests** — `pytest -q` (includes any tests the Reviewer wrote)
4. **Security scan** — `bandit -q -r . -x ./tests`

### Return contract
```python
{"passed": bool, "checks": [{"check": str, "passed": bool, "detail": str}, ...]}
```

`run_full_gate(repo_dir, target_file=None, baseline_repo_dir=None)` optionally
accepts an immutable sandbox copied before patching. When supplied, the tests
run against both baseline and candidate; only newly appearing pytest node IDs
fail the test check. Baseline infrastructure failures without parseable test
IDs and invalid baseline paths fail closed. Calls that omit the baseline retain
the original all-tests-must-pass behavior.

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

### Fail-Secure & Direct Execution
When `USE_CONTAINERIZED_GATE=false` (e.g. local dev/CI), `core/gate.py` uses direct subprocess execution with only the wall-clock timeout. 
**Crucially**, if `USE_CONTAINERIZED_GATE=true` but Docker is unavailable or crashes, the system **fails securely (fail-closed)** and rejects the patch, rather than silently falling back to host execution and breaking the security boundary.

### Standalone Package
The containerized isolation engine is also exported as a standalone Python package (`janus-sandbox` located in `packages/janus-sandbox/`), completely decoupled from the application for use in other AI agent workflows.

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
- **Call graph neighbors** (`_find_call_graph_neighbors`): Python callers and
  callees use AST-derived symbols, one hop in each direction, so comments and
  string literals do not create false edges. Non-Python files use a conservative
  identifier fallback; malformed Python is fail-soft. A Reviewer that can't see
  callers can't tell if a signature change breaks something three files away.
- **Prior fix commits** (`_find_prior_fixes`): `git log` on the target file,
  filtered to messages containing a fix-related keyword
  (`fix`, `bug`, `patch`, `issue`, `crash`, `regression`, `hotfix`). A bug
  fixed once and reintroduced is a very high-value catch. Structural scans use
  the mutable candidate sandbox; when it has no `.git`, history can be read
  from the original source repository. Missing history still degrades to an
  empty list, not an error.
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
- Python caller/callee matching now uses AST-derived symbols, while
  non-Python matching remains a conservative regex fallback. Aliased imports,
  dynamic dispatch, and full language-semantic resolution remain out of scope.
- A materialized candidate without `.git` can use the original source repo for
  prior-fix history; repositories with no usable source history still yield no
  prior-fix signal.

- Every signal is best-effort and degrades independently — a Reviewer with
  partial repo context should still be better off than one with none, but
  none of this is exhaustive.
- **Graceful degradation for non-Python repos**: `repo_context.py` degrades gracefully without throwing errors.

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
store is a 25-example seed set (§5's known limits), and repository-context
signals are not a corpus-level static-analysis benchmark. Fine-tuning stays
deferred until growing and hardening both of these is a worse investment than
starting on learned review skill directly — that point has not been reached.
The implemented `training/dataset.py` boundary rejects retrieval-only records:
future examples must carry repository/PR/review/commit provenance and executable
before/after evidence. It exports provider-neutral JSONL but does not train,
upload, or claim model weights.

To replace the retrieval-augmented Reviewer with a fine-tuned one once that
point is reached:

1. **Training data**: Mine PR review comments from a large set of repos.
   For each comment, retain repository/PR/review IDs, source and fix commits,
   the exact counterexample command/path, and independently reproduced
   before/after outcomes. `training/dataset.py` validates this boundary and
   `scripts/prepare_reviewer_dataset.py` exports provider-neutral JSONL; it
   does not fabricate missing lineage or launch a training job.

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
# For BYOK setup:
# echo "OPENAI_API_KEY=..." >> .env
# echo "ANTHROPIC_API_KEY=..." >> .env

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
| `ADMIN_API_KEYS` | No | — | `key1:operator1,key2:operator2` — admin-only keys for `/admin/debates`; empty disables admin access |
| `DATABASE_URL` | Yes | `sqlite:///./adversarial_code_review.db` | DB connection string |
| `ADV_REVIEW_MODEL` | No | `gemini-2.5-flash` | LLM model name |
| `USE_CONTAINERIZED_GATE` | No | `false` | Enable Docker sandbox |
| `SANDBOX_IMAGE` | If containerized | `adv-review-sandbox:latest` | Sandbox Docker image |
| `OPENAI_API_KEY` | No | — | BYOK API key |
| `ANTHROPIC_API_KEY` | No | — | BYOK API key |
| `GITHUB_WEBHOOK_SECRET` | No | — | HMAC-SHA256 secret for verifying GitHub webhook signatures |
| `GITHUB_APP_ID` | For GitHub App mode | — | Numeric GitHub App ID for installation-token minting |
| `GITHUB_APP_PRIVATE_KEY` | For GitHub App mode | — | PEM private key injected from a deployment secret manager; never persist it in the database or logs |
| `GITHUB_APP_JWT_TTL_SECONDS` | No | `540` | Short-lived App JWT lifetime, capped by GitHub’s limit |
| `GITHUB_TOKEN_CACHE_SKEW_SECONDS` | No | `60` | Refresh installation tokens before expiry |
| `GITHUB_TOKEN` | Legacy only | — | Static PAT fallback for unscoped, single-tenant deployments |
| `LLM_CALL_TIMEOUT_SECONDS` | No | `180` | Maximum duration of one async LLM/MCP agent call |

### janus.yaml Configuration
Per-repo configuration via `janus.yaml` allows developers to set custom validation checks, trigger modes, BYOK models (for enterprise users), and more.

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
6. **Zombie Sessions:** If a worker crashes mid-debate (e.g., OOM kill), the debate may stay in `running` status indefinitely. A manual DB cleanup or cron sweeper is recommended for production.

---

## 9. Hard Rules

These are non-negotiable constraints enforced in code:

1. **The gate decides.** No LLM output can override `run_full_gate()`. A patch
   merges if and only if `final_gate["passed"] == True`.

2. **The Reviewer cannot write source files.** Its MCP `tool_filter` excludes
   any tool that writes to source files. It can only write test files via
   `write_candidate_test`.

3. **The Reviewer cannot call `run_full_gate`.** It cannot approve a merge.

4. **Patcher never runs unless Reviewer found concrete issue.** The debate is Reviewer-first.

5. **No hardcoded secrets.** All secrets flow through `core/config.py` and
   environment variables, including BYOK keys. API keys are hashed at rest. No plaintext keys
   in code, config files, or logs.

6. **Untrusted code runs in containers.** When `USE_CONTAINERIZED_GATE=true`,
   all gate commands run in network-isolated, resource-capped containers. If Docker is unavailable, the gate fails securely (fail-closed) rather than silently degrading to host execution.

7. **Every round is persisted immediately.** If a worker crashes mid-debate,
   all completed rounds are recoverable from the database.

8. **Claims are atomic.** `claim_queued_session()` uses DB-level locking to
   prevent double-processing across parallel workers.

9. **No fine-tuning claims.** The codebase and all documentation must not
   claim fine-tuned model weights exist. The Reviewer's quality comes from
   retrieval-augmented few-shot grounding. Fine-tuning is future work.

10. **Fork PRs are untrusted by default.** Untrusted code from external forks will not execute in the sandbox without explicit configuration.

11. **Debate engine is language-agnostic.** Validation rules and prompts support generalized `{language}` injections.

12. **User's model choice is invisible to debate engine.** BYOK configuration operates completely independently from the reasoning loop.
