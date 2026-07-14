# Janus — Architecture

This document describes how Janus is actually built, as of this revision — not
the aspirational version. Where something is a known gap or an open question
rather than a settled design, it's marked as such. See `ROADMAP.md` for what's
deliberately deferred and why.

---

## 1. What Janus is

Janus runs adversarial code-review debates between two LLM agents — a
**Patcher** that proposes a fix, and a **Reviewer** that tries to disprove it —
refereed by a **deterministic gate** (lint, type check, tests, security scan)
that is the only thing with actual merge authority. Neither agent can merge
its own work; the gate's verdict is final.

The system is a REST API + background worker, not a CLI or a bot. Clients
enqueue a debate via `POST /debates` and poll `GET /debates/{id}` (or receive
a webhook / PR comment) for the result.

---

## 2. High-level flow

```
Client                API                  DB              Worker
  │                    │                    │                 │
  │  POST /debates      │                    │                 │
  ├────────────────────>│                    │                 │
  │                    │  INSERT (queued)    │                 │
  │                    ├───────────────────>│                 │
  │  {debate_id, queued}│                    │                 │
  │<────────────────────┤                    │                 │
  │                    │                    │   poll (1-5s)    │
  │                    │                    │<─────────────────┤
  │                    │                    │  UPDATE (running)│
  │                    │                    │<─────────────────┤
  │                    │                    │                 │  sandbox_copy()
  │                    │                    │                 │  run_debate()
  │                    │                    │   per-round      │  ├─ Patcher patches
  │                    │                    │   UPDATE/INSERT  │  ├─ Reviewer critiques
  │                    │                    │<─────────────────┤  ├─ gate checks
  │                    │                    │                 │  └─ repeat or stop
  │                    │                    │  final UPDATE    │
  │                    │                    │<─────────────────┤  (merged/rejected/error)
  │  GET /debates/{id}  │                    │                 │
  ├────────────────────>│  SELECT            │                 │
  │                    ├───────────────────>│                 │
  │  {status, rounds…}  │                    │                 │
  │<────────────────────┤                    │                 │
```

The API and worker are **separate processes**, communicating only through the
database — never in-process function calls. This is deliberate: it's the only
way a worker crash (killed process, not just a Python exception) doesn't take
the API down with it, and it's what makes horizontal worker scaling possible.
It's also the seam where a real bug was found and fixed late in this project's
life — see §9.3.

---

## 3. Component map

```
adversarial_code_review/
├── api/
│   ├── app.py              FastAPI app: POST /debates, GET /debates/{id},
│   │                       GET /healthz, GET /metrics
│   ├── schemas.py           Request/response models + validation
│   │                       (repo_ref allowlist, target_file denylist,
│   │                       pr_repo/pr_number/webhook_url cross-validation)
│   └── auth.py              API-key → tenant_id resolution, rate limiting
│
├── core/
│   ├── orchestrator.py       The debate loop itself (run_debate)
│   ├── agents.py             Patcher/Reviewer agent construction,
│   │                       instructions, MCP tool_filters
│   ├── gate.py               Deterministic checks: lint, type check, tests,
│   │                       security scan, sandbox lifecycle, container
│   │                       isolation, path validation
│   ├── retrieval.py          Behavioral retrieval — "what does a real catch
│   │                       look like" (ChromaDB + sentence-transformers)
│   ├── repo_context.py       Repository-context retrieval — "what does THIS
│   │                       repo actually look like" (call graph, git
│   │                       history, test conventions)
│   ├── llm_client.py         Multi-API-key pool with round-robin + cooldown
│   ├── notifications.py      PR comments + webhooks, with SSRF protection
│   ├── path_safety.py        repo_ref / target_file validation shared by
│   │                       the API layer and the orchestrator
│   ├── worker.py             Poll loop, concurrency control, zombie-session
│   │                       sweeper, graceful shutdown
│   ├── config.py             All settings, env-var driven, one place
│   └── observability.py      Structured logging, metrics, cost tracking
│
├── storage/
│   ├── models.py             DebateSession, Round (SQLAlchemy ORM)
│   └── db.py                 Engine/session management, migrations,
│                             claim_queued_session, sweep_zombie_sessions
│
├── mcp_server/
│   └── server.py             Exposes gate.py's functions as MCP tools the
│                             agents call directly (sandbox_copy, run_linter,
│                             run_type_check, run_tests, run_security_scan,
│                             run_full_gate, write_candidate_test,
│                             run_candidate_test)
│
├── retrieval_pipeline/
│   ├── schema.py             Validated record shape for behavioral examples
│   └── ingest.py              Batch ingestion into the ChromaDB store
│
├── packages/janus-sandbox/    Standalone, zero-dependency extraction of the
│                             container-isolation logic — independently
│                             installable, not coupled to the rest of Janus
│
├── data/
│   └── real_catch_examples.seed.jsonl   Seed set for behavioral retrieval
│
├── demo_repo/                 Intentionally buggy reference fixture used by
│                             the eval suite and local dev
│
└── evals/                     One eval file per major subsystem (see §10)
```

---

## 4. The debate loop (`core/orchestrator.py`)

`run_debate(repo_dir, target_file, ticket, debate_id=None, tenant_id=None)` is
the core function. Structure:

1. **Validate `repo_dir`** against `ALLOWED_REPO_ROOTS` (`path_safety.validate_repo_ref`)
   — defense-in-depth; the API layer already validated this, but `run_debate`
   doesn't trust that it was always called through the API (see §7.1).
2. **Create the sandbox** (`gate.sandbox_copy`) — a full copy of the repo in
   an isolated temp directory. Everything from here on operates on the
   sandbox, never the original.
3. **Persist session start** (`_persist_session_start`) — an **upsert**, not
   an insert (see §9.3 for why this matters).
4. **Patcher writes the initial patch.** The Patcher can only ever write to
   `target_file` — there is no other write path in the whole function. This
   single-file-write invariant is what makes gate-check *scoping* (§5.3)
   semantically sound, not just a shortcut.
5. **Round loop** (up to `MAX_ROUNDS`, default 5), each round:
   - Retrieve behavioral examples (`retrieval.retrieve_examples`) and
     repository context (`repo_context.retrieve_repo_context`) — two
     independent sources (§6), re-fetched every round since the code under
     review changes each round.
   - Build a **fresh Reviewer agent** for this round, with both retrieved
     contexts rendered into distinct prompt slots.
   - Reviewer critiques. If it finds a real issue, it must write an
     executable counterexample and confirm it fails via `run_candidate_test`
     — not the general `run_tests` sweep (see §5.4 for why this distinction
     is load-bearing, not stylistic).
   - Run the gate (`run_full_gate`), scoped to `target_file` for the static
     checks (§5.3).
   - Persist the round (`_persist_round`) — every round, not just the final
     one, so an in-flight debate survives a crash without losing history.
   - Detect two silent-failure modes and record them rather than let them
     pass unnoticed: `code_extraction_failed` (Patcher's response had no
     parseable code block) and `reviewer_skipped_counterexample` (Reviewer
     gave prose critique but never wrote a test).
   - Stop if the Reviewer says "no further issues," or `MAX_ROUNDS` is hit.
   - Otherwise, the Patcher gets the critique and tries again.
6. **Final gate run**, same scoping. `result.merged = final_gate["passed"]`
   — the gate's verdict, not either agent's opinion, decides the outcome.
7. **Persist session end** and fire notifications (§8) if configured.
8. **`finally`: remove the sandbox.** This is a hard guarantee — wrapped
   around the *entire* debate body (`_run_debate_inner`), not scattered
   `rmtree` calls at each anticipated failure branch. Verified by forcing a
   completely unanticipated exception mid-debate and confirming cleanup
   still ran.

### 4.1 LLM calls: retry, circuit breaker, key rotation

Every LLM call goes through `_ask()`, which layers:
- **Retry with exponential backoff** (3 attempts, 1s/2s/4s) on any exception.
- **A circuit breaker** — after repeated consecutive failures, subsequent
  calls fail fast rather than retry into a known-down dependency.
- **Key rotation on rate-limit errors** (`llm_client.is_rate_limit_error`):
  the Reviewer draws a fresh key from the pool every round (it's rebuilt
  fresh each round anyway); the Patcher draws one key per debate and only
  rotates mid-debate on an actual 429, since its session persists across
  rounds and rebuilding it loses no state — every prompt already carries the
  full ticket + current code, so nothing is lost by starting a fresh session
  bound to a new key.
- **Cost tracking** per call, aggregated per debate, with a per-key breakdown
  (`calls_per_key`) so usage skew across pooled keys is visible without ever
  logging a raw key.

---

## 5. The gate (`core/gate.py`)

The gate is the only thing with merge authority. Four checks:

| Check | Tool | Scoped to `target_file`? |
|---|---|---|
| Lint | `ruff check` | Yes |
| Type check | `mypy --ignore-missing-imports --follow-imports=silent` | Yes |
| Tests | `pytest -q` | **No — always the full suite** |
| Security scan | `bandit` | Yes |

### 5.1 Container isolation

When `USE_CONTAINERIZED_GATE=true`, every check runs inside a fresh Docker
container: `--network none`, memory/CPU/PID limits, `--read-only` on
everything except the mounted repo dir and `/tmp`. This is what actually
stops an adversarial or simply broken patch from doing anything to the host.

**Fail-closed, not fail-open**: if containerization is enabled but Docker is
unavailable, the gate raises an error and fails the check — it does not
silently fall back to running untrusted code natively on the host. This
was a real, since-fixed vulnerability: the original implementation caught
`FileNotFoundError` (Docker missing) and quietly executed on the host
instead.

### 5.2 repo_dir validation — two distinct checks, two distinct meanings

Every function in `gate.py` is exposed directly as an MCP tool the Patcher
and Reviewer can call with arguments **they generate themselves** — not just
whatever `orchestrator.py` passes in. This was a real, critical vulnerability
until fixed: an agent could call `run_tests(repo_dir="/etc")` directly, and
nothing checked it.

Two different validations, because `repo_dir` means two different things
depending on the function:

- **`sandbox_copy(repo_dir)`** — `repo_dir` is an *original source path*.
  Validated against `ALLOWED_REPO_ROOTS` via `path_safety.validate_repo_ref`
  — the same allowlist check already applied at the API layer, reused here
  so there is only ever one definition of "an allowed source repo."
- **Every other function** (`run_linter`, `run_type_check`, `run_tests`,
  `run_security_scan`, `write_candidate_test`, `run_candidate_test`) —
  `repo_dir` is a *sandbox path*, already isolated. Validated with a looser
  check: it must resolve under the OS temp directory. Deliberately looser
  than requiring `sandbox_copy`'s exact `adv_review_sandbox_` prefix, so it
  doesn't reject legitimate ad hoc temp dirs (e.g. test fixtures) — but
  still fully closes the actual reported vulnerability, since `/etc`,
  `/home`, and `C:\Windows` can never resolve under a temp directory.

### 5.3 Why scoping lint/type/security to `target_file` is sound, not a shortcut

`run_linter`, `run_type_check`, and `run_security_scan` accept an optional
`target_file` and scope the underlying tool invocation to just that file.
This closed two real bugs found by running the gate against a real external
repository (`pytest-dev/pluggy`), not a synthetic fixture:

1. **`mypy .` crashes outright** ("Duplicate module named") on any repo with
   two files sharing a module name in different directories — a common
   pattern (multiple example subprojects, each with their own `setup.py`).
   Scoping to one file sidesteps whole-tree package resolution entirely.
2. Even scoped, mypy's default import-following surfaced a *pre-existing,
   unrelated* error in a file `target_file` imports. Fixed with
   `--follow-imports=silent`, which still follows imports for accurate
   cross-file type checking but suppresses errors from files other than
   the one actually being reviewed.

This scoping is **not a compromise that trades correctness for convenience**:
the Patcher can only ever write to `target_file` — there is no other write
path in `run_debate` — so nothing else in the sandbox ever changes. A patch
cannot introduce a new lint/type/security issue anywhere it didn't touch.
Scanning just `target_file` is *complete* for these three checks, not partial.

`run_tests` is **deliberately not scoped this way**. It's the one check that
operates at runtime across the whole call graph, and a patch to `target_file`
can genuinely break a test that exercises a different file entirely — the
exact cross-file breakage `repo_context.py`'s call-graph retrieval exists to
help the *Reviewer* anticipate. Narrowing test execution would require
guessing a test-file naming convention (the same fragile pattern that caused
the bug in §5.4) in exchange for silently losing real regression detection.

**Known, explicitly unsolved gap**: because `run_tests` runs unscoped, a repo
with pre-existing failing tests unrelated to any patch will never pass the
gate — confirmed concretely on `pluggy`, which has 5 failing tests on its own
unmodified `main`. Scoping doesn't fix this for tests; only comparing against
a baseline run of the unpatched repo would. See `ROADMAP.md`.

### 5.4 `write_candidate_test` / `run_candidate_test` — why two tools, not one

The Reviewer proves a critique is real by writing an executable
counterexample (`write_candidate_test`) and running it
(`run_candidate_test`) — not the general `run_tests` sweep. This split fixes
a serious, verified bug: `write_candidate_test` hardcodes writing to
`tests/{filename}`, but many real repos configure an explicit `testpaths`
(pytest.ini / tox.ini / pyproject.toml) that excludes that directory. On
such a repo, `run_tests`'s bare `pytest -q` respects that config and **never
executes the file the Reviewer just wrote** — confirmed by reproducing this
exactly against `pluggy`, whose `tox.ini` restricts `testpaths` to
`testing/`. The Reviewer would believe it had proof of a bug; the gate would
never see it.

`run_candidate_test` runs the Reviewer's exact file by explicit path
(`pytest <exact path>`), which always collects it regardless of the target
repo's own `testpaths` — pytest only applies `testpaths` when invoked with
no path arguments at all. Both functions share one path-resolution helper
(`_resolve_candidate_test_path`) so they can never target different files.

---

## 6. Retrieval — two independent systems

Deliberately two separate modules, two separate mechanisms, injected into
two separate prompt slots (`{retrieved_examples}` and `{repo_context}`) —
conflating them would make both harder to reason about and debug.

### 6.1 Behavioral retrieval (`core/retrieval.py`)

Answers "what does a real catch look like." A ChromaDB persistent vector
store of historical review comments that preceded real bug-fix commits.
Seeded from `data/real_catch_examples.seed.jsonl` (25 curated examples),
grown via `retrieval_pipeline/ingest.py` without downtime. Embeddings
computed locally (`sentence-transformers`, `all-MiniLM-L6-v2`) — the model
weights need a one-time network download on first use, but query-time
retrieval itself never requires network access.

This is explicitly a **stand-in for a future fine-tuned Reviewer**, not a
permanent architecture — see `ROADMAP.md` §Fine-tuning.

### 6.2 Repository-context retrieval (`core/repo_context.py`)

Answers "what does *this* repo actually look like." Re-read fresh from the
live sandbox every round (so it always reflects the current patch), three
signals:
- **Call graph neighbors** — AST-based, one hop: which files reference
  `target_file`'s definitions, and what `target_file` calls that it doesn't
  define. Verified against `pluggy`: correctly found all 19 files in the
  repo referencing `_hooks.py`'s definitions.
- **Prior fix commits** — `git log` on `target_file`, filtered to messages
  containing a fix-related keyword. A bug fixed once and reintroduced is a
  high-value catch. Requires the sandbox to actually be a git repo (it
  usually is — `shutil.copytree` preserves `.git`; this only fails if the
  *source* repo itself has no git history, which was previously
  misdocumented as a `sandbox_copy` limitation — it isn't one).
- **Test conventions** — samples existing test files to inform the
  Reviewer's writing style. Checks `settings.REPO_CONTEXT_TEST_DIR_NAMES`
  (default: `tests`, `testing`, `test`) in order, not hardcoded to `tests`
  only — a real bug, confirmed on `pluggy` (which uses `testing/`), where
  the hardcoded version silently found zero samples despite 9 real test
  files existing.

Every signal degrades independently and silently on failure (unparseable
code, no git history, no test directory) rather than failing the whole
call — partial repo context is still better than none.

---

## 7. Security model

Security fixes accumulated across multiple audit passes; consolidated here
rather than scattered by discovery order.

### 7.1 Path/repo validation (`core/path_safety.py`)

Two validators, applied at multiple layers deliberately (defense-in-depth,
not redundancy for its own sake):
- `validate_repo_ref(repo_ref)` — fail-closed allowlist against
  `ALLOWED_REPO_ROOTS`. Empty allowlist rejects everything, not silently
  allows it. Applied at: the API request schema (fast 422), the
  orchestrator (before `sandbox_copy`), and `gate.sandbox_copy` itself
  (since it's independently reachable via MCP).
- `looks_like_path_traversal(target_file)` — best-effort denylist
  (rejects absolute paths and `..` components) usable *before* a sandbox
  exists. The authoritative check happens later, once the sandbox path is
  known, via `resolve()` + `is_relative_to()`.

### 7.2 SSRF protection (`core/notifications.py`)

`webhook_url` is attacker-controlled (any tenant can set it on their own
debate). `post_webhook` resolves the hostname and rejects the request if
*any* resolved address is private, loopback, link-local, reserved,
multicast, or unspecified — checked for every A/AAAA record, not just the
first. This directly closes the classic cloud-metadata SSRF vector
(`169.254.169.254`), verified against that exact address.

**Documented, not closed**: DNS rebinding (a hostname resolves safely at
check time, then a malicious DNS server returns a different address at
request time). Closing this fully requires pinning the validated IP and
connecting to it directly — real, more invasive work, tracked in
`ROADMAP.md`.

### 7.3 Sandbox escape via MCP tools

Covered in §5.2. Worth restating here: this was the most severe finding
across all audit passes, because it bypassed every other security
measure — an agent didn't need to exploit anything about the *debate logic*
to reach arbitrary host paths, just call an MCP tool with a different
argument than the orchestrator intended.

### 7.4 Zombie session recovery (`storage/db.py`, `core/worker.py`)

If a worker process is killed outright (OOM, SIGKILL, hardware failure)
mid-debate, nothing in the process survives to update its `DebateSession`
row — it stays `status='running'` forever. `sweep_zombie_sessions`, run
periodically from the worker's own poll loop, finds and marks these
`'error'` rather than silently re-queuing them (a crash can be caused by
something inherent to the debate itself — a pathological repo, a
memory-exhausting loop — and blind re-queuing could retry a poisoned debate
forever). Correctly distinguishes a genuine zombie from a healthy,
still-in-progress multi-round debate by checking the more recent of the
session's `updated_at` (only touched at claim/completion) or its latest
round's `created_at` (touched every round).

---

## 8. Notifications (`core/notifications.py`)

Two optional, independent side effects fired after a debate completes —
a debate with neither configured behaves exactly as if this feature didn't
exist:

- **GitHub PR comment**, if `pr_repo` + `pr_number` were both provided.
  Uses the Issues API (`/issues/{number}/comments`), not the Check Runs
  API — a Check Run gives richer pass/fail UI but requires a GitHub App
  installation-token flow, meaningfully more setup than a plain PAT. A
  reasonable future upgrade if that UI is wanted badly enough (see
  `ROADMAP.md`).
- **Webhook POST**, if `webhook_url` was provided (or `DEFAULT_WEBHOOK_URL`
  is configured server-side as a fallback).

Every notification call is best-effort: failures are logged and swallowed,
never raised. A broken webhook or an expired GitHub token must not make an
already-successful, already-persisted debate look like it failed.

---

## 9. Data model and persistence

### 9.1 `DebateSession` / `Round` (`storage/models.py`)

`DebateSession` — one row per debate: status (`queued`/`running`/`merged`/
`rejected`/`error`), repo/target/ticket, tenant, PR/webhook fields, final
gate result and cost as JSON, error message.

`Round` — one row per debate round: patch text, reviewer text, gate result,
which behavioral examples and repo-context signals were retrieved,
extraction-failure and skipped-counterexample flags. Persisted after
*every* round, not just at completion, so an in-flight debate is inspectable
and recoverable even if the process dies mid-debate.

### 9.2 SQLite vs. Postgres

`DATABASE_URL` supports both. Two SQLite-specific fixes worth knowing about:

- **In-memory SQLite (`sqlite:///:memory:` or bare `sqlite:///`) requires
  `StaticPool`.** Without it, every new connection checkout gets its own
  private, empty database — migrations create tables on one connection,
  requests query a different, tableless one. `storage/db.py` detects this
  case and applies `StaticPool` automatically.
- **`DateTime(timezone=True)` columns don't round-trip timezone info on
  SQLite** (they do on Postgres) — a value written as timezone-aware UTC
  comes back naive on read. `_ensure_aware_utc` in `storage/db.py`
  normalizes this before any datetime comparison (used by the zombie
  sweeper); comparing an aware and naive datetime otherwise raises
  `TypeError`.

### 9.3 The upsert bug — found only by actually running the system

`_persist_session_start` originally did an unconditional `INSERT`. This is
correct only when `run_debate` creates the very first row for a debate ID —
true when called directly (as `eval_reviewer.py` does, with no `debate_id`
argument, so a fresh UUID is generated). It is **not** true in the real
system flow: the API's `POST /debates` already inserts the row with
`status='queued'`, and the worker's `claim_queued_session` has already
`UPDATE`d it to `status='running'` by the time `run_debate` is called at
all. The unconditional insert collided with that already-existing row's
primary key **every single time a debate ran through the real API+worker
path** — meaning the system's actual, documented way of being used was
non-functional, and no unit test caught it, because the one test that
exercises the full debate logic never goes through the API+worker claim
sequence first.

Fixed: `_persist_session_start` now upserts — updates the existing row if
one exists for this ID, creates one only if none does. Verified against a
real, separate-process API + worker + SQLite-file run, not mocked.

This is the clearest example in the project of why integration testing
against the real, separate-process topology matters more than any amount
of unit testing against mocked or in-process pieces — see `ROADMAP.md`
for the state of that testing effort and what's still open.

---

## 10. Testing

One eval file per major subsystem, all explicitly listed in
`.github/workflows/ci.yml` (not relying on directory-based pytest
collection — this was tried and found to silently collect zero tests
against this project's `evals/__init__.py` + `pyproject.toml` `testpaths`
combination on newer pytest versions):

| File | Covers |
|---|---|
| `eval_gate.py` | Gate checks, container isolation, scoping, candidate-test execution, repo_dir validation |
| `eval_repo_context.py` | Call graph, prior fixes, test conventions |
| `eval_retrieval.py` | Behavioral retrieval store, ingestion pipeline |
| `eval_llm_client.py` | Key pool round-robin, cooldown, rate-limit classification |
| `eval_notifications.py` | PR comments, webhooks, SSRF protection |
| `eval_path_safety.py` | repo_ref allowlist, target_file denylist |
| `eval_api.py` | Auth, rate limiting, request validation, the enqueue/status happy path |
| `eval_storage_db.py` | Zombie-session sweeper |
| `eval_reviewer.py` | Integration test — full debate against a real Gemini API key, marked `pytest.mark.integration` |

A recurring theme worth naming: several real bugs (a broken relationship on
a frozen-dataclass settings singleton, a naive/aware datetime comparison,
the upsert collision) were only found by *actually executing* code against
real dependencies — a real SQLite file, a real external repo, a real
separate-process worker — not by code review or mocked unit tests alone.
`eval_reviewer.py` is the one test that exercises the full debate loop with
a real LLM, and it's also the one test whose call pattern (`run_debate`
called directly, no `debate_id`) happens to sidestep the most serious bug
found this project. That's not a criticism of the test — it's a reminder
that unit tests validate components, and only running the real topology
validates the seams between them.

---

## 11. Known open items

Tracked in detail in `ROADMAP.md`. Summary:

- **Unresolved**: in a live end-to-end run, `_persist_session_end` was
  observed to not complete within the full worker process after a real
  (failing) LLM call sequence, despite completing correctly and quickly in
  isolation. Root cause not yet isolated; the call is now wrapped so a
  persist failure is logged rather than silently lost, but this is not
  confirmed fixed.
- **Deliberately deferred**: fine-tuning the Reviewer, an admin
  dashboard, DNS-rebinding hardening on webhooks, baseline-diffing the
  gate's test check (so pre-existing repo debt doesn't block unrelated
  patches forever), and every non-REST-API integration surface (GitHub
  App, CI/CD step, IDE extension, CLI).
