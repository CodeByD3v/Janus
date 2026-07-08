# Adversarial Code Review — Patcher vs. Reviewer Agents

<div align="center">
    <img alt="Janus" src="https://github.com/user-attachments/assets/925849f9-1ab4-4a39-be99-0c264601a5ba" />
</div>

A production service for adversarial code-review debates. A Patcher agent
proposes fixes, a Reviewer agent critiques them with executable
counterexamples, and a deterministic gate (real lint/type/test/security
tooling) has sole merge authority. Deployed as a multi-tenant API with
persistence, observability, and container-isolated gate execution.

```
Patcher proposes → Reviewer critiques (with a failing test it wrote and ran)
  → Patcher fixes or pushes back → repeat (≤5 rounds) → deterministic gate
  → MERGE or REJECT
```

---

## What This Is — and What It Isn't

**What is real and deployed:**

- **Structural role asymmetry** enforced via MCP tool filters. The Reviewer
  literally cannot edit source files or call `run_full_gate` — this is
  enforced by the tool dispatch, not by prompt instruction.
- **A Reviewer grounded in retrieval-augmented few-shot examples** pulled
  from a persistent, growable vector store of historical "real catch" review
  comments. The store starts with 25 curated examples and grows via batch
  ingestion (`retrieval_pipeline/ingest.py`) without service restarts.
- **A deterministic gate** (ruff, mypy, pytest, bandit) executed inside
  locked-down, resource-capped Docker containers. The gate is the ONLY
  thing with merge authority — not the LLMs.
- **Production infrastructure**: persistent DB (SQLite dev / Postgres prod),
  authenticated API, per-key rate limiting, structured logging, Prometheus
  metrics, cost tracking, queue-based workers with atomic claiming.

**What is explicitly future work:**

- Fine-tuning the Reviewer on a large mined dataset of PR review comments
  that historically preceded a real bug-fix commit. The retrieval store is
  the seam where that future dataset plugs in. See
  [AGENTS.md](AGENTS.md) § Fine-Tuning Interface.

---

## Project Layout

```
├── core/                              core engine package
│   ├── config.py                      env-driven settings (single import point)
│   ├── observability.py               structured JSON logging, metrics, cost tracking
│   ├── gate.py                        deterministic gate with containerized execution
│   ├── agents.py                      Patcher + Reviewer (MCP tool_filter asymmetry)
│   ├── orchestrator.py                debate loop (retry, circuit breaker, persistence)
│   ├── retrieval.py                   ChromaDB persistent vector store + retrieval
│   └── worker.py                      DB-polling queue consumer (atomic claiming)
│
├── api/
│   ├── app.py                         FastAPI (POST /debates, GET /debates/{id}, healthz, metrics)
│   ├── auth.py                        API key auth + per-key rate limiting
│   └── schemas.py                     Pydantic request/response models
│
├── storage/
│   ├── models.py                      DebateSession + Round ORM models (SQLAlchemy)
│   └── db.py                          Engine, session factory, atomic claiming
│
├── retrieval_pipeline/
│   ├── schema.py                      RealCatchExample Pydantic model
│   └── ingest.py                      Batch ingestion CLI
│
├── data/
│   └── real_catch_examples.seed.jsonl 25 curated "real catch" examples
│
├── mcp_server/server.py               FastMCP stdio server (gate tools for agents)
├── demo_repo/                         Intentionally buggy inventory module
├── evals/                             eval_gate, eval_retrieval, eval_api, eval_reviewer
│
├── Dockerfile                         Service image (API + worker)
├── docker/sandbox.Dockerfile          Locked-down gate-execution image
├── docker-compose.yml                 Local dev stack (builds from source)
├── docker-compose.prod.yml            Production stack (pulls CI-built images, GAP 16)
├── .github/workflows/                 CI (lint/type/evals) and deploy (build/push/migrate/roll out)
└── AGENTS.md                          Full operational reference
```

---

## Quick Start

### Run the deterministic gate alone (no API key needed)

```bash
pip install -r requirements.txt
python -m core.gate
```

Runs lint/type/test/security checks against `demo_repo/`. On the unmodified
demo repo this **passes** despite two real bugs — the existing test suite is
deliberately weak. That gap is exactly what the Reviewer agent closes.

### Local development (builds images from source)

```bash
# 1. Set secrets
echo "GOOGLE_API_KEY=your-gemini-key" > .env
echo "API_KEYS=your-api-key:your-tenant-id" >> .env

# 2. Build the sandbox image
docker compose --profile build build sandbox-builder

# 3. Start API + worker + Postgres
docker compose up --build
```

This builds from source every time — fine for local dev, not what production
deploys run. See **Production Deployment** below for the CI-built,
CI-tested path.

### Seed the retrieval store

The store is auto-seeded on first boot from `data/real_catch_examples.seed.jsonl`.
To add more examples:

```bash
python -m retrieval_pipeline.ingest path/to/new_examples.jsonl
```

---

## API Usage

### Enqueue a debate

```bash
curl -X POST http://localhost:8000/debates \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "repo_ref": "demo_repo",
    "target_file": "inventory.py",
    "ticket": "average_price() should return 0.0 for an empty list. apply_bulk_discount() must not mutate the caller input."
  }'
# → {"debate_id": "...", "status": "queued"}
```

### Poll debate status

```bash
curl http://localhost:8000/debates/{debate_id} -H "X-API-Key: $KEY"
# → Full debate state: status, rounds, gate results, cost
```

### Health check

```bash
curl http://localhost:8000/healthz
# → {"status": "healthy", "db_reachable": true, "sandbox_image_present": true}
```

### Metrics

```bash
curl http://localhost:8000/metrics
# → Prometheus-format counters and histograms
```

---

## Architecture

| Concept | Implementation |
|---|---|
| **Agent / Multi-agent system (ADK)** | `core/agents.py` — two `LlmAgent`s, each with its own `InMemoryRunner` and session in `core/orchestrator.py` |
| **MCP Server** | `mcp_server/server.py` — `FastMCP` stdio server; both agents connect via `MCPToolset` with different `tool_filter`s |
| **Retrieval-Augmented Generation** | `core/retrieval.py` — ChromaDB persistent store, per-round retrieval, few-shot injection into Reviewer's instruction |
| **Structural asymmetry** | MCP `tool_filter` enforces different capabilities — the Reviewer cannot write source or call `run_full_gate` |
| **Deterministic gate** | `core/gate.py` — ruff/mypy/pytest/bandit, optionally containerized with `--network none`, memory/CPU/PID limits |
| **Persistence** | `storage/` — SQLAlchemy ORM, per-round persistence, survives crashes |
| **API** | `api/app.py` — FastAPI, async debate enqueue, tenant-isolated reads |
| **Auth + rate limiting** | `api/auth.py` — hashed API keys, per-key token bucket |
| **Worker** | `core/worker.py` — DB-polling, atomic claiming, configurable concurrency |
| **Observability** | `core/observability.py` — structured JSON logging, Prometheus metrics, cost tracking |
| **Container isolation** | `docker/sandbox.Dockerfile` + `core/gate._run_containerized()` |

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_API_KEY` | Worker | — | Gemini API key |
| `API_KEYS` | API | — | `key:tenant,key:tenant` |
| `DATABASE_URL` | Yes | `sqlite:///./adversarial_code_review.db` | DB connection |
| `ADV_REVIEW_MODEL` | No | `gemini-2.5-flash` | LLM model |
| `USE_CONTAINERIZED_GATE` | No | `false` | Docker sandbox |
| `SANDBOX_IMAGE` | If containerized | `adv-review-sandbox:latest` | Sandbox image |
| `ADV_REVIEW_MAX_ROUNDS` | No | `5` | Debate round cap |
| `CHROMA_PERSIST_DIR` | No | `./chroma_store` | Vector store path |
| `LOG_LEVEL` | No | `INFO` | Log level |
| `WORKER_POLL_INTERVAL` | No | `5` | Worker poll seconds |
| `WORKER_MAX_CONCURRENT` | No | `4` | Max parallel debates |

---

## Production Deployment

`.github/workflows/deploy.yml` builds and pushes both images, runs DB
migrations, then **actually rolls the new images out** — it doesn't stop at
"pushed to a registry" (GAP 16).

### Chosen target: a single VM via SSH + docker-compose, not Kubernetes

The worker mounts the Docker socket to spawn sandbox containers for the gate
(`gate.py`'s container isolation — see `docker-compose.prod.yml`'s `worker`
service). That one dependency shapes the whole deployment decision:

- **Serverless platforms are out.** Cloud Run and Fargate (without
  privileged mode) don't allow a container to spawn sibling containers —
  the gate's per-check isolation model doesn't fit that execution model at
  all, not just awkwardly.
- **Kubernetes is possible, but not free.** Running the worker as a pod that
  can spawn sandbox containers means either a Docker-in-Docker sidecar or
  mounting the host's Docker socket into the pod — both require the pod to
  run **privileged**, which most managed clusters (GKE Autopilot, EKS with
  Pod Security Standards enforced, etc.) restrict or block outright for
  good reasons. If you go this route, budget for a dedicated node pool with
  relaxed pod security policy for worker pods specifically, and treat that
  node pool as a smaller trust boundary than the rest of the cluster.
- **A plain VM avoids the tradeoff entirely** — the whole point of choosing
  it here. `docker-compose.prod.yml` mirrors `docker-compose.yml`'s
  topology but pulls pre-built, pre-tested images from the registry instead
  of building from source on the deploy host.

If your priorities differ — you're already running Kubernetes for
everything else, or you need the deploy host itself to be untrusted — the
Kubernetes path is real and buildable, it's just a genuinely different
security posture than what's implemented here, not a drop-in swap.

### Required GitHub secrets

| Secret | Required | Description |
|---|---|---|
| `DEPLOY_HOST` | Yes | Deploy target's hostname/IP |
| `DEPLOY_USER` | Yes | SSH user on the deploy host |
| `DEPLOY_SSH_KEY` | Yes | Private key for that user (add the public half to the host's `authorized_keys`) |
| `DEPLOY_PATH` | Yes | Directory on the host where `docker-compose.prod.yml` lives and `.env` is managed |
| `DEPLOY_PORT` | No | SSH port, defaults to `22` |
| `DATABASE_URL` | Yes | Used by the `migrate` job — same Postgres the deployed stack connects to |
| `DOCKER_REGISTRY` | No | Defaults to `ghcr.io` |
| `DOCKER_IMAGE_PREFIX` | No | Defaults to the GitHub repo name |
| `DOCKER_USERNAME` / `DOCKER_PASSWORD` | No | Defaults to `github.actor` / `GITHUB_TOKEN`, fine for GHCR |

### One-time deploy host setup

```bash
# On the deploy host:
mkdir -p /opt/janus && cd /opt/janus
# Real secrets live here, managed directly on the host — the deploy
# pipeline never writes secrets to disk itself, only pulls images and
# restarts containers.
cat > .env <<'EOF'
GOOGLE_API_KEYS=key-one,key-two
API_KEYS=your-api-key:your-tenant-id
EOF
```
Point `DEPLOY_PATH` at `/opt/janus` (or wherever you chose). After that,
every push to `main` builds, pushes, migrates, and rolls out automatically
— `deploy.yml`'s last step polls `/healthz` and fails the job loudly if the
new containers don't come up healthy within 50 seconds, rather than
silently leaving a broken deploy running.

---


```bash
# Pure-logic tests (no API key, no Docker needed)
pytest evals/eval_gate.py -v
pytest evals/eval_retrieval.py -v
pytest evals/eval_api.py -v

# Containerized gate tests (requires Docker)
pytest evals/eval_gate.py -v -k "Containerized"

# Integration test (requires GOOGLE_API_KEY)
GOOGLE_API_KEY=your-key pytest evals/eval_reviewer.py -v -m integration
```

Both `ruff check .` and `mypy --ignore-missing-imports .` should pass clean.

---

## Design Honesty

This project does NOT claim to have fine-tuned two models with different
incentives. It IS claiming to have built:

1. **Structural role asymmetry** enforced via MCP tool filters — not prompts
2. **A Reviewer that can only prove bugs exist** via executable
   counterexamples — it cannot fix code
3. **A deterministic gate** executed in isolated containers — the only thing
   with merge authority, not the LLMs
4. **RAG-augmented Reviewer** grounded in a persistent, growable retrieval
   store of historical review comments
5. **Production infrastructure** around all of the above: persistence,
   authenticated API, observability, concurrency, CI/CD

Fine-tuning a Reviewer on a large mined PR dataset is explicit **future
work** — the retrieval store starts curated and is designed to grow, but
nothing here claims fine-tuned weights exist. See
[AGENTS.md](AGENTS.md) for the full contract and fine-tuning interface spec.

---

## Key Rotation

1. Add new key to `API_KEYS`: `oldkey:tenant,newkey:tenant`
2. Restart the API (rolling restart is safe)
3. Migrate callers to the new key
4. Remove old key

---

## Known Limitations

- Seed retrieval store has 25 examples — quality improves as it grows
- `MAX_ROUNDS = 5` is not calibrated against measured false-positive rates
- Demo scope is a single Python file; no corpus-level evaluation yet
- The Reviewer sometimes gives prose critiques without executable tests
  (tracked via `reviewer_skipped_counterexample` metric)
- Circuit breaker thresholds are not auto-tuned
