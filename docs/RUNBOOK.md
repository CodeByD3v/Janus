# Janus — Runbook

This is the operational reference: how to set this up, run it, test it, and
operate it, exactly as the repo exists today. For *why* it's built this way,
see `docs/ARCHITECTURE.md`. For the current status and deliberate future
work, see `docs/Roadmap.md`.

`ALLOWED_REPO_ROOTS` is required for requests that reference local
repositories because `repo_ref` validation is fail-closed. The setup examples
below set it explicitly; the README environment table also documents it.

---

## 1. Current status, in one paragraph

Janus 2.0 is implemented and regression-tested. The REST API, deterministic
gate with isolation and baseline diffing, both retrieval systems, multi-key
and multi-provider LLM support, GitHub App integration, notifications,
admin visibility, auto-merge controls, stricter Reviewer executable-evidence
normalization, configurable debate controls, multi-language regression fixtures,
and the documented security fixes are in place. The `_persist_session_start`
insert-vs-upsert collision was found through a real API+worker run and fixed.
Async persistence is thread-offloaded and timeout-bounded, MCP teardown is
shielded and bounded, and lifecycle regressions cover event-loop continuity. A
provider-neutral training-data boundary is implemented, but model fine-tuning
is not performed. Optional diagnosis of third-party MCP root causes and live
Kubernetes/GitHub infrastructure validation remain documented in
`docs/Roadmap.md`.

---

## 2. Prerequisites

- Python 3.12
- Docker (required for `USE_CONTAINERIZED_GATE=true`, which is what
  production and the dev docker-compose stack both use; optional for a
  quick manual run with it left `false`)
- A real Gemini API key (`GOOGLE_API_KEY`) to run an actual debate. Not
  needed to run the gate alone, the eval suite (minus `eval_reviewer.py`),
  or to enqueue/inspect debates via the API without a worker attached.
- `janus.yaml` (optional, for custom validation checks).

---

## 3. First-run setup

```bash
git clone <repo> && cd Janus
pip install -r requirements.txt
```

Two things almost everything below needs, set as environment variables or
in a `.env` file depending on how you're running it:

```bash
export GOOGLE_API_KEY=your-gemini-key          # needed to actually run a debate
export API_KEYS=your-api-key:your-tenant-id     # needed to call the API at all
# Optional operator access; omit to keep the admin tier disabled.
export ADMIN_API_KEYS=your-admin-key:operations
export ALLOWED_REPO_ROOTS=/absolute/path/to/Janus   # required for local repo_ref validation

```

`ALLOWED_REPO_ROOTS` must be an absolute path (or comma-separated list of
them) that the `repo_ref` you send actually resolves under. Pointing it at
the repo root covers `demo_repo` and any other fixture inside it.

Note: you can optionally provide a `janus.yaml` file in the repo root to configure custom validation checks.

---

## 4. Running it

### 4.1 Just the gate (no API key, no Docker needed)

The fastest way to see the deterministic gate work at all:

```bash
python -m core.gate
```

Runs lint/type/test/security checks against `demo_repo/`. This **passes**
on the unmodified demo repo despite two real, seeded bugs — the existing
test suite there is deliberately weak. That gap between "gate passes" and
"code is actually correct" is exactly what the Reviewer agent exists to
close.

### 4.2 Full stack, local dev (Docker, builds from source)

```bash
# 1. Secrets
echo "GOOGLE_API_KEY=your-gemini-key" > .env
echo "API_KEYS=your-api-key:your-tenant-id" >> .env
echo "ADMIN_API_KEYS=your-admin-key:operations" >> .env   # optional
echo "ALLOWED_REPO_ROOTS=$(pwd)" >> .env

# 2. Build the sandbox image once
docker compose --profile build build sandbox-builder

# 3. Start API + worker + Postgres
docker compose up --build
```

This builds every image from source on every run — correct for local dev,
not what production actually deploys (see §6). `docker-compose.yml`'s
`api` and `worker` services both hardcode `USE_CONTAINERIZED_GATE: "true"`
and mount `/var/run/docker.sock` into the worker so it can spawn sandbox
containers — Docker must be running on the host for this stack to work at
all.

The Compose file forwards `API_KEYS`, `ADMIN_API_KEYS`,
`ALLOWED_REPO_ROOTS`, and the other required settings into the API service.
Inspect the resolved configuration with `docker compose config` before
starting a deployment; keep real credentials in the host `.env` and never
commit them.

### 4.3 Manual run, no Docker (fastest local loop, gate runs unsandboxed)

Useful for iterating without waiting on image builds. `USE_CONTAINERIZED_GATE`
stays at its default (`false`), so the gate runs checks as direct
subprocesses on your host instead of inside a container — fine for trusted
local code, not a substitute for the real isolation guarantee in any
shared or production environment.

```bash
# Terminal 1 — API
export DATABASE_URL=sqlite:///./janus_local.db
export API_KEYS=local-key:local-tenant
export ALLOWED_REPO_ROOTS=$(pwd)
uvicorn api.app:app --reload

# Terminal 2 — worker
export DATABASE_URL=sqlite:///./janus_local.db
export GOOGLE_API_KEY=your-gemini-key
export ALLOWED_REPO_ROOTS=$(pwd)
python -m core.worker
```

Both processes must point at the **same** `DATABASE_URL` — they only ever
communicate through the database, never directly (see
`docs/ARCHITECTURE.md` §2).

### 4.4 Seeding / growing the retrieval store

Auto-seeded on first boot from `data/real_catch_examples.seed.jsonl` — no
action needed for a first run. To add more curated examples later, without
downtime:

```bash
python -m retrieval_pipeline.ingest path/to/new_examples.jsonl
```

### 4.5 Using the API once it's running

```bash
# Enqueue
curl -X POST http://localhost:8000/debates \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{
    "repo_ref": "demo_repo",
    "target_file": "inventory.py",
    "ticket": "average_price() should return 0.0 for an empty list. apply_bulk_discount() must not mutate the caller input."
  }'
# → {"debate_id": "...", "status": "queued"}

# Poll
curl http://localhost:8000/debates/{debate_id} -H "X-API-Key: $KEY"

# Health
curl http://localhost:8000/healthz

# Metrics (Prometheus format)
curl http://localhost:8000/metrics
```

`repo_ref` in the example above (`"demo_repo"`) is resolved relative to
wherever the worker process's current working directory is — if you're
running the manual (non-Docker) setup from the repo root, `demo_repo` alone
works as long as `ALLOWED_REPO_ROOTS` covers it (§3). If it doesn't
resolve, pass an absolute path instead.

### 4.6 Configuring janus.yaml for custom validation

You can configure custom validation checks by placing a `janus.yaml` file in the repository root:

```yaml
# janus.yaml — placed in the repository root
validation:
  checks:
    - name: lint
      command: ruff check .
    - name: typecheck
      command: mypy --ignore-missing-imports .
    - name: test
      command: pytest -q
    - name: security
      command: bandit -q -r . -x ./tests
```

If no `janus.yaml` exists, the current Python defaults are used automatically.

---

## 5. Testing

### 5.1 Run every eval file exactly as CI does

CI invokes each file as its **own separate `pytest` command** — not a
combined directory run. This isn't a style choice: directory-based
collection (`pytest evals/`) was found to silently collect zero tests
against this project's `evals/__init__.py` + `pyproject.toml` `testpaths`
combination on newer pytest. Running files together in one process can
also surface unrelated shared-state flakiness between files (seen during
development with `eval_api.py`) that never occurs when each runs alone —
if you see a failure running multiple files together that doesn't
reproduce running that one file by itself, it's very likely that, not a
real regression. Always verify a suspicious failure by running the single
file alone before treating it as real.

```bash
# No real GOOGLE_API_KEY needed for these evals; CI discovers every
# evals/eval_*.py module and excludes only the integration marker:
$env:GOOGLE_API_KEYS="dummy-a,dummy-b"
pytest evals/eval_*.py -q -m "not integration"

# No real GOOGLE_API_KEY needed for these older individual commands:
# eval_llm_client.py

# constructs a real KeyPool object in some tests, which requires *a*
# key to exist even though it's never actually called, so set a dummy
# value for that one:
pytest evals/eval_gate.py -v
pytest evals/eval_retrieval.py -v
pytest evals/eval_api.py -v
pytest evals/eval_repo_context.py -v
GOOGLE_API_KEYS="dummy-a,dummy-b" pytest evals/eval_llm_client.py -v
pytest evals/eval_notifications.py -v
pytest evals/eval_path_safety.py -v
pytest evals/eval_storage_db.py -v

# Needs a real GOOGLE_API_KEY — runs an actual full debate end to end:
pytest evals/eval_reviewer.py -v -m integration
```

`eval_gate.py` has two tests gated on Docker being available
(`@pytest.mark.skipif` on a live Docker check) — they skip cleanly,
not fail, when Docker isn't present.

### 5.2 What each file actually covers

The committed Python/TypeScript/Go corpus fixtures are regression fixtures,
not a benchmark over many real repositories. `eval_kubernetes.py` validates
offline Kustomize rendering and boundary invariants; it does not replace a live
cluster rollout.

| File | Covers | Needs Docker | Needs `GOOGLE_API_KEY` |
|---|---|---|---|
| `eval_gate.py` | Lint/type/test/security checks, container isolation, check scoping, `run_candidate_test`, `repo_dir` validation | Partially (2 tests) | No |
| `eval_retrieval.py` | Behavioral retrieval store, ingestion pipeline | No | No |
| `eval_api.py` | Auth, rate limiting, request validation, enqueue/status happy path | No | No |
| `eval_repo_context.py` | Call graph, prior-fix detection, test-convention discovery | No | No |
| `eval_llm_client.py` | Key pool round-robin, cooldown, rate-limit classification | No | A dummy value works — `GOOGLE_API_KEYS=a,b` is enough; some tests construct a real `KeyPool`, which requires *a* key to exist even though it's never called |
| `eval_notifications.py` | PR comments, webhooks, SSRF protection | No | No |
| `eval_path_safety.py` | `repo_ref` allowlist, `target_file` denylist | No | No |
| `eval_storage_db.py` | Zombie-session sweeper | No | No |
| `eval_reviewer.py` | Full debate loop, real LLM calls | No | **Yes, a real one** |

**Known fragility, not a Janus bug**: `eval_llm_client.py::test_keyed_gemini_binds_the_given_key`
depends on `google.adk.models.Gemini` having a private attribute
(`_base_url_and_api_version`) that `core/llm_client.py`'s `_KeyedGemini`
override relies on. This has been observed to both pass and fail against
what `pip show google-adk` reports as the identical pinned version
(`1.3.0`), in different environment states — almost certainly package
resolution drift, not a real regression. If this specific test fails,
check `python -c "from google.adk.models import Gemini; print('_base_url_and_api_version' in dir(Gemini))"`
before assuming Janus's own code broke. This dependency on a private,
underscore-prefixed third-party attribute is a genuine fragility worth
knowing about even when the test is passing — see `core/llm_client.py`'s
module docstring for why this override exists at all.

### 5.3 One-off sanity checks worth knowing about

```bash
# Import chain sanity check — catches circular imports or missing deps fast
python -c "
import core.config, core.path_safety, core.notifications, core.llm_client
import core.repo_context, core.retrieval, core.gate, core.agents
import core.orchestrator, core.worker, core.observability, core.diagnostics
import storage.models, storage.db
import api.schemas, api.app, api.auth
import mcp_server.server
print('clean')
"

# Compile-check everything without running it
python -m py_compile $(find . -name "*.py" ! -path "*/packages/*")
```

---

## 6. Deploying

### 6.1 CI (`.github/workflows/ci.yml`)

Lint, type check, and the eight non-integration eval files run on every
PR. `eval_reviewer.py` runs only if a `GOOGLE_API_KEY` secret is
configured on the repo — gated correctly via `secrets.GOOGLE_API_KEY` in
the step's `if:` condition (a step's own `env:` block is *not* visible to
that same step's `if:` — this was a real, since-fixed bug in an earlier
version of this pipeline).

The GitHub App regression module is intentionally logic-level coverage:
`eval_github_app.py` mocks `_github_get` while testing signature verification,
trigger parsing, installation metadata validation, and file filtering. CI does
not claim to validate a real installed App delivering to a public endpoint and
reviewing a real repository; that requires external GitHub infrastructure and
remains an operational follow-up.

### 6.2 Production deploy (`.github/workflows/deploy.yml`)

On push to `main`: builds and pushes both images (service + sandbox),
migrates the database, then **actually rolls the new images out** and
health-checks the result — it doesn't stop at "pushed to a registry."

Target: a single VM via SSH + `docker-compose.prod.yml`; the worker needs to
spawn sibling sandbox containers, which serverless platforms don't allow and
many managed Kubernetes clusters restrict. The implemented Kubernetes
alternative is below. See README.md's "Production Deployment" section for the
full VM reasoning and required GitHub secrets.

### 6.3 Kubernetes alternative (`k8s/`)

The Kubernetes path is an explicit alternative, not part of the current SSH
workflow. It uses one API replica until Chroma multiwriter behavior is proven,
one worker on a dedicated tainted Docker-socket node pool, RWX claims for the
shared Chroma/repository-cache volumes, and a service image containing the
Docker CLI. The API overrides `USE_CONTAINERIZED_GATE=false`; only the worker
uses the host socket.

Create `janus-runtime-secrets` out of band, set the worker deployment's
`supplementalGroups` to the numeric Docker-socket group for the chosen node
pool, and run the ordered release script:

```bash
k8s/deploy.sh "$GIT_COMMIT_SHA"
```

The script applies Postgres infrastructure, waits for its rollout, runs the
immutable-tagged migration Job to completion, and only then applies and waits
for the API/worker deployments. `kubectl apply -k k8s/` intentionally excludes
the migration Job and should not be treated as an ordered release. The sandbox
can only render these manifests offline; target-cluster server-side validation,
RWX storage, node permissions, ingress, and production rollout remain pending.

---

## 7. Async lifecycle hardening and optional diagnosis

The previously observed persistence/event-loop issue is mitigated in the
current runtime. Persistence, worker database operations, gate execution, and
sandbox work are moved off the event loop; LLM streams have configured
deadlines; and MCP teardown is bounded with shielded cleanup. Regression tests
cover event-loop continuity and sync/async teardown. The original diagnostic
material remains below only if operators need to investigate a third-party MCP
compatibility issue in a persistent environment; it is not a prerequisite for
normal Janus operation.

```bash
export GOOGLE_API_KEY=invalid-test-key   # fails fast — skip waiting on a real network timeout
export ALLOWED_REPO_ROOTS=$(pwd)/demo_repo
bash scripts/reproduce_persist_hang.sh
```

Full instructions — including exactly when to attach `py-spy` from a
second terminal and what to look for in its output — are in the script's
own header comment (`scripts/reproduce_persist_hang.sh`). In short:
watch for `debate_failed_initial_patch` in the output, note the worker PID
the script prints just before that, and run `py-spy dump --pid <PID>` a
few times from a second terminal while the debate's status is still
`running`.

Optional richer trace (off by default, safe to leave off for normal use):

```bash
export DIAGNOSTIC_PERSIST_TRACE=true
```

Writes raw, synchronous, `fsync`'d trace lines to `/tmp/janus_persist_trace.log`
(configurable via `DIAGNOSTIC_PERSIST_TRACE_PATH`), bypassing the logging
framework and asyncio entirely — see `core/diagnostics.py`'s docstring for
why that distinction matters for this specific investigation.

---

## 8. Full environment variable reference

Grouped as in `core/config.py`. `—` means no default (empty string), not
"unset."

### Multi-Provider LLM (Janus 2.0)
| Variable | Default | Notes |
|---|---|---|
| `JANUS_DEFAULT_PROVIDER` | `google` | Default LLM provider for free-tier users |
| `JANUS_DEFAULT_MODEL` | `gemini-2.0-flash` | Default model for free-tier users |
| `JANUS_CONFIG_PATH` | `janus.yaml` | Path to repo-level config file |

### LLM
| Variable | Default | Notes |
|---|---|---|
| `ADV_REVIEW_MODEL` | `gemini-2.5-flash` | |
| `GOOGLE_API_KEY` | — | Single-key fallback if `GOOGLE_API_KEYS` isn't set |
| `GOOGLE_API_KEYS` | — | Comma-separated pool, takes precedence over the singular var |
| `GOOGLE_API_KEY_COOLDOWN_SECONDS` | `30` | How long a rate-limited key is skipped |

### Debate
| Variable | Default | Notes |
|---|---|---|
| `ADV_REVIEW_MAX_ROUNDS` | `5` | Must be at least 1; maximum debate rounds |
| `ADV_REVIEW_CIRCUIT_FAILURE_THRESHOLD` | `5` | Must be at least 1; consecutive failures before open |
| `ADV_REVIEW_CIRCUIT_COOLDOWN_SECONDS` | `60` | Must be non-negative; delay before one half-open probe |

### Database
| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./adversarial_code_review.db` | In-memory SQLite URLs auto-detect and use `StaticPool` |

### Behavioral retrieval
| Variable | Default | Notes |
|---|---|---|
| `CHROMA_PERSIST_DIR` | `./chroma_store` | |
| `CHROMA_COLLECTION` | `real_catch_examples` | |
| `SEED_DATA_PATH` | `core/data/real_catch_examples.seed.jsonl` | |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Downloaded once on first use, cached after |

### Repository-context retrieval
| Variable | Default | Notes |
|---|---|---|
| `REPO_CONTEXT_MAX_FILES_SCANNED` | `200` | |
| `REPO_CONTEXT_MAX_PRIOR_FIXES` | `5` | |
| `REPO_CONTEXT_MAX_TEST_SAMPLES` | `5` | |
| `REPO_CONTEXT_SNIPPET_CHARS` | `400` | |
| `REPO_CONTEXT_GIT_TIMEOUT` | `10` | |
| `REPO_CONTEXT_FIX_KEYWORDS` | `fix,bug,patch,issue,crash,regression,hotfix` | |
| `REPO_CONTEXT_TEST_DIR_NAMES` | `tests,testing,test` | Checked in order, first match wins |

### Sandbox / gate execution
| Variable | Default | Notes |
|---|---|---|
| `USE_CONTAINERIZED_GATE` | `false` | `true` in both docker-compose files |
| `SANDBOX_IMAGE` | `adv-review-sandbox:latest` | |
| `SANDBOX_MEMORY_LIMIT` | `512m` | |
| `SANDBOX_CPU_LIMIT` | `1` | |
| `SANDBOX_PID_LIMIT` | `128` | |
| `SANDBOX_TIMEOUT` | `120` | Wall-clock timeout, layered on top of the container's own limits |

### API
| Variable | Default | Notes |
|---|---|---|
| `API_HOST` | `0.0.0.0` | |
| `API_PORT` | `8000` | |
| `API_KEYS` | — | `key1:tenant1,key2:tenant2` — tenant keys; missing means tenant requests are rejected |
| `ADMIN_API_KEYS` | — | `key1:operator1,key2:operator2` — admin-only keys for `/admin/debates`; never reuse tenant keys |
| `RATE_LIMIT_REQUESTS` | `60` | Applies per tenant/operator identity |

| `RATE_LIMIT_WINDOW_SECONDS` | `60` | |
| `ALLOWED_REPO_ROOTS` | — | **Fail-closed**: empty rejects every `repo_ref`. See the note at the top of this file. |
| `CORS_ALLOWED_ORIGINS` | — | Empty disables CORS entirely; `*` is safe here since auth is header-based, not cookies |

Open `/admin` for the operator dashboard. It displays only non-sensitive debate summaries and sends the entered admin key in the `X-API-Key` header. The underlying `GET /admin/debates` endpoint supports `tenant_id`, `status`, `limit`, and `offset` filters. Ordinary tenant keys receive `403`, and an unset `ADMIN_API_KEYS` value leaves the endpoint inaccessible.

### Worker
| Variable | Default | Notes |
|---|---|---|
| `WORKER_POLL_INTERVAL` | `5` | seconds |
| `WORKER_MAX_CONCURRENT` | `4` | |
| `ZOMBIE_SESSION_TIMEOUT_MINUTES` | `30` | |
| `ZOMBIE_SWEEP_INTERVAL_SECONDS` | `300` | |

### Observability
| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `METRICS_ENABLED` | `true` | |
| `DIAGNOSTIC_PERSIST_TRACE` | `false` | See §7 |
| `DIAGNOSTIC_PERSIST_TRACE_PATH` | `/tmp/janus_persist_trace.log` | |

### Notifications
| Variable | Default | Notes |
|---|---|---|
| `GITHUB_TOKEN` | — | PAT with `repo` scope; enables PR comments |
| `GITHUB_API_URL` | `https://api.github.com` | Override for GitHub Enterprise |
| `DEFAULT_WEBHOOK_URL` | — | Fallback if a request doesn't set its own |
| `NOTIFICATION_TIMEOUT_SECONDS` | `10` | |

### MCP
| Variable | Default | Notes |
|---|---|---|
| `MCP_SERVER_SCRIPT` | `core/mcp_server/server.py`'s absolute path | Rarely needs overriding |

---

## 9. Quick troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| Every `POST /debates` returns 422 on `repo_ref` | `ALLOWED_REPO_ROOTS` unset or doesn't cover the path you sent | §3, §8 |
| Every request returns 401 | `API_KEYS` unset, or the key you're sending doesn't match | §8 |
| A debate stays `queued` forever | No worker running, or its `DATABASE_URL` doesn't match the API's | §4.3 |
| A debate stays `running` forever | Check `GET /debates/{id}` for `error_message`; if the worker crashed, `sweep_zombie_sessions` recovers it within `ZOMBIE_SESSION_TIMEOUT_MINUTES` — or you've hit §7 | §7, `docs/Roadmap.md` §2 |
| `mypy`/`ruff`/`bandit` "not found" errors from the gate | Running without Docker and without those tools installed locally | §4.3, or set `USE_CONTAINERIZED_GATE=true` with Docker running |
| `eval_retrieval.py` fails on model download | No network access to huggingface.co (sandboxed CI runners, restricted networks) | One-time download, cached after; not a code bug |
| Combined `pytest evals/` run collects 0 tests | Known pytest/`testpaths` interaction — always invoke files individually | §5.1 |
| `eval_llm_client.py`'s `KeyPool`-related tests fail | No `GOOGLE_API_KEY`/`GOOGLE_API_KEYS` set at all | §5.1 |
| `eval_llm_client.py::test_keyed_gemini_binds_the_given_key` fails specifically | Possible `google-adk` package resolution drift — see §5.2's note before assuming a real regression | §5.2 |
| `janus.yaml` validation checks failing | Commands in janus.yaml are incorrect or tools not installed | §4.6 |
| Multi-provider model errors | ADK/LiteLLM compatibility issue, or invalid BYOK API key | §8 |
