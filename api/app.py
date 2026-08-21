"""
api/app.py — FastAPI application for the Adversarial Code Review service.

Endpoints:
- POST /debates — enqueue a new debate (non-blocking)
- GET /debates/{debate_id} — get current debate state
- GET /healthz — liveness/readiness check
- GET /metrics — Prometheus-compatible metrics

All endpoints require a valid, rate-limited API key via the X-API-Key
header (except /healthz and /metrics).
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from api.auth import key_store, require_admin_api_key, require_api_key
from api.schemas import (
    AdminDebateListResponse,
    AdminDebateSummary,
    CreateDebateRequest,
    CreateDebateResponse,
    DebateResponse,
    ErrorResponse,
    HealthResponse,
    RoundResponse,
)
from core.config import settings
from core.observability import get_logger, metrics
from storage.db import get_session, run_migrations
from storage.models import DebateSession

logger = get_logger(__name__)

app = FastAPI(
    title="Adversarial Code Review API",
    description=(
        "Production API for adversarial code-review debates. "
        "A Patcher agent proposes fixes, a Reviewer agent critiques them "
        "with executable counterexamples, and a deterministic gate has "
        "sole merge authority."
    ),
    version="1.0.0",
)

# CORS: opt-in via CORS_ALLOWED_ORIGINS, disabled by default. Safe to set
# to "*" specifically because auth here is header-based (X-API-Key), never
# cookies — allow_credentials is always False, so a wildcard origin has
# nothing to ride on. See core/config.py's CORS_ALLOWED_ORIGINS docstring.
_cors_origins = settings.cors_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

from api.github_app import github_router

app.include_router(github_router)


_ADMIN_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Janus Admin Dashboard</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 0; padding: 2rem; background: #101827; color: #e5e7eb; }
    main { max-width: 1180px; margin: auto; }
    h1 { margin-top: 0; }
    form { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1.5rem 0; }
    input, button { border: 1px solid #475569; border-radius: .4rem; padding: .65rem .8rem; font: inherit; }
    input { background: #1e293b; color: inherit; }
    button { background: #2563eb; color: white; cursor: pointer; }
    button:hover { background: #1d4ed8; }
    table { width: 100%; border-collapse: collapse; background: #172033; }
    th, td { text-align: left; padding: .7rem; border-bottom: 1px solid #334155; vertical-align: top; }
    th { color: #93c5fd; }
    .status { margin: .75rem 0; min-height: 1.4rem; }
    .error { color: #fca5a5; }
    .muted { color: #94a3b8; }
    @media (max-width: 800px) { body { padding: 1rem; } table { display: block; overflow-x: auto; white-space: nowrap; } }
  </style>
</head>
<body>
<main>
  <h1>Janus Admin Dashboard</h1>
  <p class="muted">Cross-tenant debate summaries. The key is kept in memory and sent only as <code>X-API-Key</code>.</p>
  <form id="filters">
    <input id="key" type="password" autocomplete="off" placeholder="Admin API key" required>
    <input id="tenant" maxlength="128" placeholder="Tenant filter (optional)">
    <select id="status"><option value="">All statuses</option><option>queued</option><option>running</option><option>completed</option><option>error</option></select>
    <button type="submit">Refresh</button>
  </form>
  <div id="statusMessage" class="status"></div>
  <table>
    <thead><tr><th>ID</th><th>Tenant</th><th>Status</th><th>Repository</th><th>PR</th><th>Verdict</th><th>Created</th></tr></thead>
    <tbody id="rows"><tr><td colspan="7" class="muted">Authenticate to load debates.</td></tr></tbody>
  </table>
</main>
<script>
const form = document.getElementById('filters');
const message = document.getElementById('statusMessage');
const rows = document.getElementById('rows');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  message.className = 'status'; message.textContent = 'Loading…';
  const params = new URLSearchParams({limit: '100'});
  if (document.getElementById('tenant').value) params.set('tenant_id', document.getElementById('tenant').value);
  if (document.getElementById('status').value) params.set('status', document.getElementById('status').value);
  try {
    const response = await fetch('/admin/debates?' + params, {
      headers: {'X-API-Key': document.getElementById('key').value},
      credentials: 'same-origin'
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Request failed');
    rows.replaceChildren();
    for (const item of data.items) {
      const row = document.createElement('tr');
      for (const value of [item.id, item.tenant_id || '—', item.status, item.repo_ref + ' :: ' + item.target_file,
                           item.pr_repo ? item.pr_repo + ' #' + item.pr_number : '—', item.reviewer_verdict || '—', item.created_at || '—']) {
        const cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
      }
      rows.appendChild(row);
    }
    if (!data.items.length) rows.innerHTML = '<tr><td colspan="7" class="muted">No debates found.</td></tr>';
    message.textContent = `${data.total} debate(s)`;
  } catch (error) {
    message.className = 'status error'; message.textContent = error.message;
  }
});
</script>
</body>
</html>"""


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
def admin_dashboard() -> HTMLResponse:
    """Serve the operator UI; debate data still requires an admin key."""
    return HTMLResponse(_ADMIN_DASHBOARD_HTML)


@app.get(
    "/admin/debates",
    response_model=AdminDebateListResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def list_admin_debates(
    operator_id: str = Depends(require_admin_api_key),
    tenant_filter: str | None = Query(default=None, alias="tenant_id", max_length=128),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminDebateListResponse:
    """List non-sensitive debate summaries across tenants for operators."""
    del operator_id  # authorization and rate limiting are handled by the dependency
    with get_session() as db:
        query = db.query(DebateSession)
        if tenant_filter is not None:
            query = query.filter(DebateSession.tenant_id == tenant_filter)
        if status is not None:
            query = query.filter(DebateSession.status == status)
        total = query.count()
        sessions = (
            query.order_by(DebateSession.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return AdminDebateListResponse(
            items=[
                AdminDebateSummary(
                    id=session.id,
                    tenant_id=session.tenant_id,
                    repo_ref=session.repo_ref,
                    target_file=session.target_file,
                    status=session.status,
                    merged=session.merged,
                    reviewer_verdict=session.reviewer_verdict,
                    needs_human_review=session.needs_human_review,
                    pr_repo=session.pr_repo,
                    pr_number=session.pr_number,
                    commit_sha=session.commit_sha,
                    created_at=session.created_at.isoformat() if session.created_at else None,
                    updated_at=session.updated_at.isoformat() if session.updated_at else None,
                )
                for session in sessions
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    """Initialize DB and load API keys on startup."""
    run_migrations()
    key_store.load_from_env()
    from core.retrieval import initialize_store
    initialize_store()
    logger.info("api_started", host=settings.API_HOST, port=settings.API_PORT)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post(
    "/debates",
    response_model=CreateDebateResponse,
    status_code=202,
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def create_debate(
    body: CreateDebateRequest,
    tenant_id: str = Depends(require_api_key),
) -> CreateDebateResponse:
    """Enqueue a new adversarial code review debate.

    Returns immediately with a debate_id and 'queued' status.
    The debate runs asynchronously via the worker process.

    Defined as `def`, not `async def`, on purpose: get_session() is a
    synchronous SQLAlchemy session (psycopg2-binary has no async driver
    in requirements.txt). A blocking DB call inside an `async def`
    endpoint runs directly on FastAPI's single event loop thread and
    stalls every other in-flight request for its duration. A plain `def`
    endpoint is automatically dispatched to Starlette's threadpool
    instead, so a slow query here can never freeze the whole server.
    """
    debate_id = str(uuid.uuid4())
    encrypted_model_api_key = None
    if body.model_api_key is not None:
        from core.credentials import encrypt_secret
        encrypted_model_api_key = encrypt_secret(body.model_api_key.get_secret_value())

    with get_session() as db:
        session = DebateSession(
            id=debate_id,
            repo_ref=body.repo_ref,
            target_file=body.target_file,
            ticket=body.ticket,
            status="queued",
            tenant_id=tenant_id,
            pr_repo=body.pr_repo,
            pr_number=body.pr_number,
            commit_sha=body.commit_sha,
            pr_branch=body.pr_branch,
            pr_author=body.pr_author,
            github_installation_id=body.github_installation_id,
            webhook_url=body.webhook_url,
            model_provider=body.model_provider,
            model_name=body.model_name,
            model_api_key_encrypted=encrypted_model_api_key,
        )
        db.add(session)

    logger.info(
        "debate_enqueued",
        debate_id=debate_id,
        tenant_id=tenant_id,
        repo_ref=body.repo_ref,
        target_file=body.target_file,
        pr_repo=body.pr_repo,
        pr_number=body.pr_number,
        has_webhook=bool(body.webhook_url),
    )

    return CreateDebateResponse(debate_id=debate_id, status="queued")


@app.get(
    "/debates/{debate_id}",
    response_model=DebateResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
    },
)
def get_debate(
    debate_id: str,
    tenant_id: str = Depends(require_api_key),
) -> DebateResponse:
    """Get the current state of a debate, including rounds and gate results.

    Also `def`, not `async def` — same reasoning as create_debate above.
    """
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=debate_id).first()
        if session is None:
            raise HTTPException(status_code=404, detail="Debate not found")

        # Tenant isolation: only the creating tenant can view
        if session.tenant_id and session.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="Debate not found")

        return DebateResponse(
            id=session.id,
            repo_ref=session.repo_ref,
            target_file=session.target_file,
            ticket=session.ticket,
            status=session.status,
            tenant_id=session.tenant_id,
            merged=session.merged,
            final_gate=session.final_gate,
            cost=session.cost,
            error_message=session.error_message,
            pr_repo=session.pr_repo,
            pr_number=session.pr_number,
            commit_sha=session.commit_sha,
            webhook_url=session.webhook_url,
            reviewer_verdict=session.reviewer_verdict,
            needs_human_review=session.needs_human_review,
            rounds=[
                RoundResponse(
                    round_num=r.round_num,
                    patch_text=r.patch_text,
                    reviewer_text=r.reviewer_text,
                    gate_result=r.gate_result,
                    retrieved_example_ids=r.retrieved_example_ids,
                    repo_context_signals=r.repo_context_signals,
                    stop_reason=r.stop_reason,
                    code_extraction_failed=r.code_extraction_failed,
                    reviewer_skipped_counterexample=r.reviewer_skipped_counterexample,
                    reviewer_verdict=r.reviewer_verdict,
                    created_at=(r.created_at.isoformat() if r.created_at else None),
                )
                for r in session.rounds
            ],
            created_at=(session.created_at.isoformat() if session.created_at else None),
            updated_at=(session.updated_at.isoformat() if session.updated_at else None),
        )


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Liveness and readiness check.

    Checks:
    - Database is reachable
    - Sandbox container image is present (if containerized gate is enabled)

    Also `def`, not `async def` — this one blocks on BOTH a DB round-trip
    and a `docker image inspect` subprocess call (up to a 5s timeout). As
    an `async def`, a slow Docker daemon would stall the entire API for
    up to 5 seconds on every single health check — exactly the failure
    mode a liveness probe exists to catch, not cause.
    """
    db_ok = False
    db_detail = ""
    try:
        with get_session() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
            db_ok = True
    except Exception as e:
        db_detail = str(e)

    sandbox_ok = True
    if settings.USE_CONTAINERIZED_GATE:
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", settings.SANDBOX_IMAGE],
                capture_output=True,
                text=True,
                timeout=5,
            )
            sandbox_ok = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            sandbox_ok = False

    overall = "healthy" if (db_ok and sandbox_ok) else "unhealthy"
    details = {}
    if db_detail:
        details["db_error"] = db_detail
    if not sandbox_ok:
        details["sandbox_error"] = f"Image {settings.SANDBOX_IMAGE} not found"

    return HealthResponse(
        status=overall,
        db_reachable=db_ok,
        sandbox_image_present=sandbox_ok,
        details=details or None,
    )


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Prometheus-compatible metrics endpoint."""
    return Response(
        content=metrics.prometheus_text(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
