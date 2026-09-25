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

import asyncio
import json
import subprocess
import uuid
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from starlette.responses import StreamingResponse

from api.auth import key_store, require_admin_api_key, require_api_key
from api.schemas import (
    AdminCalibrationResponse,
    AdminDebateListResponse,
    AdminDebateSummary,
    AdminHealthResponse,
    CreateDebateRequest,
    CreateDebateResponse,
    DebateResponse,
    DebateSummaryResponse,
    ErrorResponse,
    HealthResponse,
    RoundResponse,
    WorkerHeartbeatSummary,
)
from core.config import settings
from core.observability import get_logger, metrics
from core.sanitizer import sanitize_text
from storage.db import get_session, run_migrations
from storage.models import DebateSession, WorkerHeartbeat

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

from api.github_app import github_router  # noqa: E402

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
            query = query.filter(DebateSession.tenant_id == tenant_filter)  # type: ignore
        if status is not None:
            query = query.filter(DebateSession.status == status)  # type: ignore
        total = query.count()
        sessions = (
            query.order_by(DebateSession.created_at.desc())  # type: ignore
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


def _build_debate_response(session: DebateSession, sanitize: bool = True) -> DebateResponse:
    """Helper to convert a DebateSession ORM instance to a DebateResponse schema."""
    rounds = []
    for r in session.rounds:
        p_text = sanitize_text(r.patch_text) if sanitize else r.patch_text
        rev_text = sanitize_text(r.reviewer_text) if sanitize else r.reviewer_text
        rounds.append(
            RoundResponse(
                round_num=r.round_num,
                patch_text=p_text,
                reviewer_text=rev_text,
                gate_result=r.gate_result,
                retrieved_example_ids=r.retrieved_example_ids,
                repo_context_signals=r.repo_context_signals,
                stop_reason=r.stop_reason,
                code_extraction_failed=r.code_extraction_failed,
                reviewer_skipped_counterexample=r.reviewer_skipped_counterexample,
                reviewer_verdict=r.reviewer_verdict,
                created_at=(r.created_at.isoformat() if r.created_at else None),
            )
        )

    err = sanitize_text(session.error_message) if sanitize else session.error_message

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
        error_message=err,
        pr_repo=session.pr_repo,
        pr_number=session.pr_number,
        commit_sha=session.commit_sha,
        webhook_url=session.webhook_url,
        reviewer_verdict=session.reviewer_verdict,
        needs_human_review=session.needs_human_review,
        rounds=rounds,
        created_at=(session.created_at.isoformat() if session.created_at else None),
        updated_at=(session.updated_at.isoformat() if session.updated_at else None),
    )


def _get_debates_summary(tenant_id: str | None = None) -> DebateSummaryResponse:
    with get_session() as db:
        query = db.query(DebateSession)
        if tenant_id is not None:
            query = query.filter(DebateSession.tenant_id == tenant_id)

        sessions = query.all()
        total = len(sessions)
        queued = sum(1 for s in sessions if s.status == "queued")
        running = sum(1 for s in sessions if s.status == "running")
        completed = sum(1 for s in sessions if s.status == "completed")
        error = sum(1 for s in sessions if s.status == "error")
        merged = sum(1 for s in sessions if s.merged is True)

        pass_verdicts = sum(1 for s in sessions if s.reviewer_verdict == "PASS")
        issue_found = sum(1 for s in sessions if s.reviewer_verdict == "ISSUE_FOUND")
        inconclusive = sum(1 for s in sessions if s.reviewer_verdict == "INCONCLUSIVE")

        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(seconds=60)
        active_workers = (
            db.query(WorkerHeartbeat)
            .filter(WorkerHeartbeat.last_heartbeat >= cutoff)
            .count()
        )

        return DebateSummaryResponse(
            total_debates=total,
            queued_debates=queued,
            running_debates=running,
            completed_debates=completed,
            error_debates=error,
            merged_count=merged,
            pass_verdicts=pass_verdicts,
            issue_found_verdicts=issue_found,
            inconclusive_verdicts=inconclusive,
            active_workers_count=active_workers,
        )


async def _stream_debate_events(debate_id: str, tenant_id: str | None = None):
    """Server-Sent Events generator for live debate updates."""
    sent_rounds_count = 0
    while True:
        with get_session() as db:
            session = db.query(DebateSession).filter_by(id=debate_id).first()
            if session is None or (
                tenant_id is not None and session.tenant_id and session.tenant_id != tenant_id
            ):
                yield f"event: error\ndata: {json.dumps({'detail': 'Debate not found'})}\n\n"
                break

            rounds = list(session.rounds)
            status = session.status
            merged = session.merged
            verdict = session.reviewer_verdict
            error = session.error_message
            final_gate = session.final_gate

            while sent_rounds_count < len(rounds):
                r = rounds[sent_rounds_count]
                sent_rounds_count += 1
                round_data = {
                    "round_num": r.round_num,
                    "patch_text": sanitize_text(r.patch_text),
                    "reviewer_text": sanitize_text(r.reviewer_text),
                    "gate_result": r.gate_result,
                    "retrieved_example_ids": r.retrieved_example_ids,
                    "repo_context_signals": r.repo_context_signals,
                    "stop_reason": r.stop_reason,
                    "code_extraction_failed": r.code_extraction_failed,
                    "reviewer_skipped_counterexample": r.reviewer_skipped_counterexample,
                    "reviewer_verdict": r.reviewer_verdict,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                yield f"event: round\ndata: {json.dumps(round_data)}\n\n"

            if status in ("completed", "error"):
                complete_data = {
                    "id": session.id,
                    "status": status,
                    "merged": merged,
                    "reviewer_verdict": verdict,
                    "error_message": sanitize_text(error),
                    "final_gate": final_gate,
                }
                yield f"event: session_complete\ndata: {json.dumps(complete_data)}\n\n"
                break

        await asyncio.sleep(1.5)


@app.get(
    "/debates",
    response_model=AdminDebateListResponse,
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
def list_customer_debates(
    tenant_id: str = Depends(require_api_key),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> AdminDebateListResponse:
    """List customer's own debates."""
    with get_session() as db:
        query = db.query(DebateSession).filter(DebateSession.tenant_id == tenant_id)
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


@app.get(
    "/debates/summary",
    response_model=DebateSummaryResponse,
    responses={401: {"model": ErrorResponse}},
)
def get_customer_summary(
    tenant_id: str = Depends(require_api_key),
) -> DebateSummaryResponse:
    """Summary metrics scoped to the authenticated customer tenant."""
    return _get_debates_summary(tenant_id=tenant_id)


@app.get(
    "/debates/{debate_id}/stream",
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def stream_customer_debate(
    debate_id: str,
    tenant_id: str = Depends(require_api_key),
) -> StreamingResponse:
    """SSE stream for watching a customer debate live."""
    return StreamingResponse(
        _stream_debate_events(debate_id=debate_id, tenant_id=tenant_id),
        media_type="text/event-stream",
    )


@app.get(
    "/admin/summary",
    response_model=DebateSummaryResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
def get_admin_summary(
    operator_id: str = Depends(require_admin_api_key),
) -> DebateSummaryResponse:
    """Cross-tenant operator summary metrics."""
    del operator_id
    return _get_debates_summary(tenant_id=None)


@app.get(
    "/admin/debates/{debate_id}",
    response_model=DebateResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def get_admin_debate(
    debate_id: str,
    operator_id: str = Depends(require_admin_api_key),
) -> DebateResponse:
    """Get any debate session state across tenants for authorized operators."""
    del operator_id
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=debate_id).first()
        if session is None:
            raise HTTPException(status_code=404, detail="Debate not found")
        return _build_debate_response(session, sanitize=True)


@app.get(
    "/admin/debates/{debate_id}/stream",
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def stream_admin_debate(
    debate_id: str,
    operator_id: str = Depends(require_admin_api_key),
) -> StreamingResponse:
    """SSE stream for operators watching any debate live."""
    del operator_id
    return StreamingResponse(
        _stream_debate_events(debate_id=debate_id, tenant_id=None),
        media_type="text/event-stream",
    )


@app.get(
    "/admin/health",
    response_model=AdminHealthResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
def get_admin_health_details(
    operator_id: str = Depends(require_admin_api_key),
) -> AdminHealthResponse:
    """Detailed worker, sandbox, and circuit-breaker telemetry for operators."""
    del operator_id
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

    workers_list = []
    with get_session() as db:
        from datetime import timedelta
        cutoff = datetime.now(UTC) - timedelta(seconds=60)
        hb_rows = db.query(WorkerHeartbeat).all()
        for hb in hb_rows:
            is_alive = hb.last_heartbeat >= cutoff if hb.last_heartbeat else False
            workers_list.append(
                WorkerHeartbeatSummary(
                    worker_id=hb.worker_id,
                    hostname=hb.hostname,
                    pid=hb.pid,
                    status=hb.status,
                    active_debate_id=hb.active_debate_id,
                    last_heartbeat=hb.last_heartbeat.isoformat() if hb.last_heartbeat else None,
                    is_alive=is_alive,
                )
            )

    from core.orchestrator.circuit_breaker import get_circuit_breaker
    cb = get_circuit_breaker()
    circuit_breaker_open = getattr(cb, "is_open", False)

    overall = "healthy" if (db_ok and sandbox_ok) else "unhealthy"
    details = {}
    if db_detail:
        details["db_error"] = db_detail
    if not sandbox_ok:
        details["sandbox_error"] = f"Image {settings.SANDBOX_IMAGE} not found"

    return AdminHealthResponse(
        status=overall,
        db_reachable=db_ok,
        sandbox_image_present=sandbox_ok,
        active_workers_count=len([w for w in workers_list if w.is_alive]),
        workers=workers_list,
        circuit_breaker_open=circuit_breaker_open,
        details=details or None,
    )


@app.get(
    "/admin/calibration",
    response_model=AdminCalibrationResponse,
    responses={401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
def get_admin_calibration(
    operator_id: str = Depends(require_admin_api_key),
) -> AdminCalibrationResponse:
    """Reviewer calibration and evidence metrics snapshot."""
    del operator_id
    with get_session() as db:
        sessions = db.query(DebateSession).all()
        total = len(sessions)
        if total == 0:
            return AdminCalibrationResponse(
                total_debates=0,
                pass_rate=0.0,
                issue_found_rate=0.0,
                inconclusive_rate=0.0,
                avg_rounds=0.0,
                max_round_terminations=0,
                counterexample_rejections=0,
            )

        pass_cnt = sum(1 for s in sessions if s.reviewer_verdict == "PASS")
        issue_cnt = sum(1 for s in sessions if s.reviewer_verdict == "ISSUE_FOUND")
        inconclusive_cnt = sum(1 for s in sessions if s.reviewer_verdict == "INCONCLUSIVE")

        total_rounds = sum(len(s.rounds) for s in sessions)
        avg_rounds = total_rounds / total

        max_round_terms = 0
        rejections = 0
        for s in sessions:
            for r in s.rounds:
                if r.stop_reason == "max_rounds":
                    max_round_terms += 1
                if r.reviewer_skipped_counterexample or r.reviewer_verdict == "INCONCLUSIVE":
                    rejections += 1

        return AdminCalibrationResponse(
            total_debates=total,
            pass_rate=round(pass_cnt / total, 3),
            issue_found_rate=round(issue_cnt / total, 3),
            inconclusive_rate=round(inconclusive_cnt / total, 3),
            avg_rounds=round(avg_rounds, 2),
            max_round_terminations=max_round_terms,
            counterexample_rejections=rejections,
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


# ---------------------------------------------------------------------------
# Serve Web Frontend SPA if built
# ---------------------------------------------------------------------------
import os
from fastapi.staticfiles import StaticFiles

_web_dist_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web", "dist")
if os.path.isdir(_web_dist_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(_web_dist_dir, "assets")), name="assets")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def serve_spa():
        index_file = os.path.join(_web_dist_dir, "index.html")
        if os.path.exists(index_file):
            with open(index_file, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse("<h1>Janus Web Dashboard build not found</h1>", status_code=404)

