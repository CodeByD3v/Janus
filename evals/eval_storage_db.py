"""
evals/eval_storage_db.py — database layer tests, focused on
sweep_zombie_sessions (zombie-session recovery).

Covers a real, verified gap: if a worker process is killed outright
(OOM kill, SIGKILL, hardware failure) mid-debate, nothing in the process
survives to update the DebateSession row — it stays status='running'
forever. sweep_zombie_sessions() finds and recovers these.

Uses a real in-memory SQLite DB (via storage.db's StaticPool handling)
rather than mocking the DB layer, since the actual bug this suite caught
during development (naive-vs-aware datetime comparison, a real
SQLAlchemy+SQLite quirk) only surfaces against a real database round-trip
— mocking would have hidden it entirely.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from storage.db import get_session, run_migrations, sweep_zombie_sessions  # noqa: E402
from storage.models import DebateSession, Round  # noqa: E402

run_migrations()


def _make_session(
    status: str, updated_minutes_ago: float, session_id: str | None = None
) -> str:
    session_id = session_id or str(uuid.uuid4())
    with get_session() as db:
        s = DebateSession(
            id=session_id,
            repo_ref="r",
            target_file="f.py",
            ticket="t",
            status=status,
        )
        s.updated_at = datetime.now(timezone.utc) - timedelta(
            minutes=updated_minutes_ago
        )
        db.add(s)
    return session_id


def _get_status(session_id: str) -> str:
    with get_session() as db:
        s = db.query(DebateSession).filter_by(id=session_id).first()
        return s.status


def _add_round(session_id: str, minutes_ago: float, round_num: int = 1) -> None:
    with get_session() as db:
        r = Round(
            session_id=session_id,
            round_num=round_num,
            patch_text="x",
            reviewer_text="y",
        )
        r.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        db.add(r)


# ---------------------------------------------------------------------------
# Core sweep behavior
# ---------------------------------------------------------------------------

def test_sweeps_a_genuine_zombie_with_no_rounds():
    """The base case this fix exists for: a session claimed a long time
    ago, no rounds ever persisted (worker died before round 1), well past
    the timeout."""
    sid = _make_session("running", updated_minutes_ago=60)
    swept = sweep_zombie_sessions(timeout_minutes=30)
    assert swept >= 1
    assert _get_status(sid) == "error"


def test_does_not_sweep_a_healthy_long_running_debate():
    """The false-positive this fix must avoid: session.updated_at is only
    touched at claim time, never per-round — a genuinely healthy,
    still-in-progress multi-round debate must not be swept just because
    its CLAIM happened a while ago, as long as it has a recent round."""
    sid = _make_session("running", updated_minutes_ago=60)
    _add_round(sid, minutes_ago=1)
    sweep_zombie_sessions(timeout_minutes=30)
    assert _get_status(sid) == "running"


def test_does_not_sweep_a_completed_session():
    sid = _make_session("merged", updated_minutes_ago=120)
    sweep_zombie_sessions(timeout_minutes=30)
    assert _get_status(sid) == "merged"


def test_does_not_sweep_a_recently_claimed_session():
    """A session claimed moments ago, no rounds yet — not stale, must
    not be swept just for having zero rounds."""
    sid = _make_session("running", updated_minutes_ago=1)
    sweep_zombie_sessions(timeout_minutes=30)
    assert _get_status(sid) == "running"


def test_sweeps_a_session_whose_rounds_are_also_stale():
    """A debate that DID make progress, but stalled — its most recent
    round is also past the timeout, so it's a genuine zombie, not a
    healthy long-running one."""
    sid = _make_session("running", updated_minutes_ago=90)
    _add_round(sid, minutes_ago=90, round_num=1)
    _add_round(sid, minutes_ago=61, round_num=2)  # still past the 30min cutoff
    swept = sweep_zombie_sessions(timeout_minutes=30)
    assert swept >= 1
    assert _get_status(sid) == "error"


def test_error_message_explains_the_sweep():
    sid = _make_session("running", updated_minutes_ago=60)
    sweep_zombie_sessions(timeout_minutes=30)
    with get_session() as db:
        s = db.query(DebateSession).filter_by(id=sid).first()
        assert s.error_message is not None
        assert "zombie" in s.error_message.lower() or "crashed" in s.error_message.lower()


def test_sweeps_multiple_zombies_in_one_call():
    sid1 = _make_session("running", updated_minutes_ago=60)
    sid2 = _make_session("running", updated_minutes_ago=90)
    swept = sweep_zombie_sessions(timeout_minutes=30)
    assert swept >= 2
    assert _get_status(sid1) == "error"
    assert _get_status(sid2) == "error"


def test_no_running_sessions_is_a_safe_noop():
    """Sweeping when nothing NEW has gone stale must not error and must
    report 0 — self-contained: first sweep clears anything already stale
    (from earlier tests sharing this in-memory DB), then a second,
    immediate call has nothing left to find."""
    sweep_zombie_sessions(timeout_minutes=30)  # clear any pre-existing zombies
    swept_again = sweep_zombie_sessions(timeout_minutes=30)
    assert swept_again == 0


def test_returns_the_correct_count():
    sweep_zombie_sessions(timeout_minutes=30)  # clear any pre-existing zombies
    sid = _make_session("running", updated_minutes_ago=999)
    swept = sweep_zombie_sessions(timeout_minutes=30)
    assert swept == 1
    assert _get_status(sid) == "error"
