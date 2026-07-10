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
from datetime import datetime, timezone
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


def get_engine():
    """Return the SQLAlchemy engine (for testing/inspection)."""
    return _engine
