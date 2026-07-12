"""
storage/db.py — database connection, session management, and migrations.

Provides:
- Engine and session factory driven by DATABASE_URL (SQLite dev, Postgres prod)
- get_session() context manager for safe transactional access
- run_migrations() for schema creation/updates
- claim_queued_session() for safe worker concurrency (atomic claim)

Usage:
    from storage.db import get_session, run_migrations
    run_migrations()
    with get_session() as session:
        session.add(DebateSession(...))
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings
from core.observability import get_logger
from storage.models import Base, DebateSession

logger = get_logger(__name__)


def _is_in_memory_sqlite(database_url: str) -> bool:
    """True for `sqlite:///:memory:` and the ambiguous bare `sqlite:///`
    form (no path at all) — both create a private, connection-scoped
    database under SQLAlchemy's default pooling, NOT a real shared file.

    This matters: without StaticPool, every new connection checkout (e.g.
    every FastAPI request routed through Starlette's threadpool for a
    sync `def` endpoint — see api/app.py) gets its OWN fresh, empty
    database, completely disconnected from whatever run_migrations()
    created tables in. Symptom: `OperationalError: no such table` on
    every query, even immediately after a successful migration.

    A real file path (`sqlite:///./app.db`) is unaffected by this and
    does not need StaticPool — the file itself is the shared state, not
    the connection.
    """
    if not database_url.startswith("sqlite"):
        return False
    # sqlite:///:memory:  -> path component is exactly ":memory:"
    # sqlite:///           -> path component is empty
    path = database_url.split("///", 1)[-1] if "///" in database_url else ""
    return path in ("", ":memory:")


_engine_kwargs: dict = {
    "echo": False,
    "pool_pre_ping": True,
}

if settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    if _is_in_memory_sqlite(settings.DATABASE_URL):
        # StaticPool reuses a single connection for every checkout, so
        # the database migrations create and the database every request
        # queries are guaranteed to be the same one. Without this, an
        # in-memory SQLite URL is effectively unusable the moment more
        # than one connection is ever opened against it.
        _engine_kwargs["poolclass"] = StaticPool
        logger.info(
            "in_memory_sqlite_detected",
            detail="Using StaticPool so all connections share one database.",
        )

# Engine and session factory — created once at module level.
_engine = create_engine(settings.DATABASE_URL, **_engine_kwargs)
_SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False)


@contextlib.contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a transactional DB session. Commits on success, rolls back on error."""
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_migrations() -> None:
    """Create all tables if they don't exist.

    For production, this should be replaced with Alembic migrations.
    For now, this is sufficient for the first production cut.
    """
    logger.info("running_migrations", database_url=_sanitize_url(settings.DATABASE_URL))
    Base.metadata.create_all(_engine)
    logger.info("migrations_complete")


def _sanitize_url(url: str) -> str:
    """Strip credentials from a DB URL for logging."""
    if "@" in url:
        scheme_rest = url.split("://", 1)
        if len(scheme_rest) == 2:
            after_at = scheme_rest[1].split("@", 1)
            if len(after_at) == 2:
                return f"{scheme_rest[0]}://***@{after_at[1]}"
    return url


def claim_queued_session(worker_id: str) -> Optional[str]:
    """Atomically claim a queued DebateSession for processing.

    Uses an atomic UPDATE ... WHERE to prevent double-processing by
    multiple worker processes. Returns the session ID if one was claimed,
    or None if no queued sessions are available.

    For PostgreSQL, this uses UPDATE ... RETURNING for true atomicity.
    For SQLite, we use a SELECT + UPDATE within a transaction (sufficient
    for the single-writer dev case).
    """
    session = _SessionFactory()
    try:
        if settings.DATABASE_URL.startswith("sqlite"):
            # SQLite: select first queued, then update
            debate = (
                session.query(DebateSession)
                .filter(DebateSession.status == "queued")
                .order_by(DebateSession.created_at)
                .first()
            )
            if debate is None:
                return None
            debate.status = "running"  # type: ignore[assignment]
            debate.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
            session.commit()
            return debate.id
        else:
            # PostgreSQL: atomic UPDATE ... RETURNING
            result = session.execute(
                text(
                    """
                    UPDATE debate_sessions
                    SET status = 'running', updated_at = NOW()
                    WHERE id = (
                        SELECT id FROM debate_sessions
                        WHERE status = 'queued'
                        ORDER BY created_at
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id
                    """
                )
            )
            row = result.fetchone()
            session.commit()
            return row[0] if row else None
    except Exception:
        session.rollback()
        logger.warning("claim_session_failed", worker_id=worker_id, exc_info=True)
        return None
    finally:
        session.close()


def _ensure_aware_utc(dt: datetime | None) -> datetime | None:
    """Normalize a datetime to timezone-aware UTC.

    SQLAlchemy's DateTime(timezone=True) columns (used throughout
    storage/models.py) always WRITE timezone-aware UTC datetimes here,
    but SQLite — this project's documented default for local dev, and
    what this eval suite itself runs against — has no native
    timezone-aware column type and hands them back NAIVE on read,
    even though an aware datetime was stored. Comparing a naive value
    against an aware one raises TypeError. This codebase never writes
    anything but UTC, so a naive value read back is always assumed to
    have been UTC. (Postgres does not have this gap — DateTime(timezone=
    True) round-trips correctly there — but this normalization is a
    no-op for an already-aware datetime, so it's safe to apply
    unconditionally regardless of which database is in use.)
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def sweep_zombie_sessions(timeout_minutes: int) -> int:
    """Find DebateSessions stuck in status='running' with no recent
    activity and mark them 'error', freeing them from limbo.

    Fixes a real, verified gap: if a worker process is killed outright
    (OOM kill, SIGKILL, hardware failure) mid-debate, nothing in the
    still-alive process gets a chance to run — worker.py's own
    `except Exception` handler only catches exceptions within a process
    that is still executing, not the process itself dying. Without this
    sweep, such a session's status stays 'running' in the database
    forever.

    "No recent activity" means the more recent of:
    - the session's own `updated_at` (touched at claim time and at
      completion/error time, but NOT per round)
    - its most recent Round's `created_at` (touched every round — see
      orchestrator.py's per-round persistence)
    Using only session.updated_at would misfire on a genuinely healthy,
    long-running multi-round debate that just hasn't finished yet;
    checking both avoids that false positive.

    Deliberately marks swept sessions 'error' rather than resetting them
    back to 'queued' for automatic retry: a worker crash can be caused by
    something inherent to the debate itself (a pathological repo, a
    memory-exhausting agent loop), and blindly re-queuing could retry the
    same poisoned debate forever, crashing every worker that picks it up.
    'error' surfaces the problem via GET /debates/{id} instead — visible
    failure over silent infinite retry, consistent with how run_debate's
    own exception handling already marks failures this way.

    Returns the number of sessions swept.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
    swept = 0

    session = _SessionFactory()
    try:
        running = (
            session.query(DebateSession)
            .filter(DebateSession.status == "running")
            .all()
        )
        for debate in running:
            last_round_at = _ensure_aware_utc(
                max((r.created_at for r in debate.rounds if r.created_at), default=None)
            )
            last_activity = _ensure_aware_utc(debate.updated_at)
            if last_round_at is not None and (
                last_activity is None or last_round_at > last_activity
            ):
                last_activity = last_round_at

            # A running session with no timestamp at all shouldn't be
            # possible (claim_queued_session always sets updated_at) but
            # treat it as immediately stale rather than crash or skip it
            # silently if it somehow occurs.
            is_stale = last_activity is None or last_activity < cutoff
            if not is_stale:
                continue

            debate.status = "error"  # type: ignore[assignment]
            debate.error_message = (  # type: ignore[assignment]
                f"Swept by zombie-session sweeper: no activity for over "
                f"{timeout_minutes} minutes (worker likely crashed)."
            )
            debate.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
            swept += 1
            logger.warning(
                "zombie_session_swept",
                debate_id=debate.id,
                last_activity=last_activity.isoformat() if last_activity else None,
                timeout_minutes=timeout_minutes,
            )

        session.commit()
    except Exception:
        session.rollback()
        logger.error("zombie_sweep_failed", exc_info=True)
        return 0
    finally:
        session.close()

    if swept:
        logger.info("zombie_sweep_complete", swept_count=swept)
    return swept


def get_engine():
    """Return the SQLAlchemy engine (for testing/inspection)."""
    return _engine
