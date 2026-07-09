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

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

from api.auth import key_store, require_api_key
from api.schemas import (
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


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def startup() -> None:
    """Initialize DB and load API keys on startup."""
    run_migrations()
    key_store.load_from_env()
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
            webhook_url=body.webhook_url,
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
