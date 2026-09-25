"""
tests/test_api_stream.py — Test SSE streaming updates for live debate tracking.
"""

import asyncio
import uuid
import pytest
from fastapi.testclient import TestClient

from api.app import _stream_debate_events, app
from api.auth import key_store
from storage.db import get_session, run_migrations
from storage.models import DebateSession, Round


@pytest.fixture(autouse=True)
def setup_db():
    run_migrations()
    key_store.register_key("test-key", "test-tenant-123", role="tenant")
    key_store.register_key("admin-test-key", "admin-op", role="admin")


def test_sse_stream_generator_emits_in_order():
    """Test that _stream_debate_events emits rounds and completes session."""
    async def _test():
        debate_id = str(uuid.uuid4())
        tenant_id = "test-tenant-123"

        with get_session() as db:
            session = DebateSession(
                id=debate_id,
                repo_ref="demo_repo",
                target_file="inventory.py",
                ticket="Fix inventory bug",
                status="running",
                tenant_id=tenant_id,
            )
            db.add(session)

        events = []

        async def _consume_stream():
            async for chunk in _stream_debate_events(debate_id, tenant_id=tenant_id):
                events.append(chunk)

        async def _add_rounds():
            await asyncio.sleep(0.1)
            with get_session() as db:
                db.add(
                    Round(
                        session_id=debate_id,
                        round_num=1,
                        patch_text="def fix1(): pass",
                        reviewer_text="Issue found in fix1",
                        reviewer_verdict="ISSUE_FOUND",
                    )
                )
            await asyncio.sleep(0.1)
            with get_session() as db:
                db.add(
                    Round(
                        session_id=debate_id,
                        round_num=2,
                        patch_text="def fix2(): pass",
                        reviewer_text="Looks good",
                        reviewer_verdict="PASS",
                    )
                )
                s = db.query(DebateSession).filter_by(id=debate_id).first()
                if s:
                    s.status = "completed"
                    s.reviewer_verdict = "PASS"
                    s.merged = True

        await asyncio.gather(_consume_stream(), _add_rounds())

        full_stream_text = "\n".join(events)
        assert "event: round" in full_stream_text
        assert "event: session_complete" in full_stream_text
        assert "fix1" in full_stream_text
        assert "fix2" in full_stream_text

    asyncio.run(_test())



def test_sse_stream_backlog_replay():
    """Verify stream replays existing completed session rounds immediately."""
    debate_id = str(uuid.uuid4())
    tenant_id = "test-tenant-123"

    with get_session() as db:
        session = DebateSession(
            id=debate_id,
            repo_ref="demo_repo",
            target_file="inventory.py",
            ticket="Fix bug",
            status="completed",
            reviewer_verdict="PASS",
            merged=True,
            tenant_id=tenant_id,
        )
        db.add(session)
        db.add(
            Round(
                session_id=debate_id,
                round_num=1,
                patch_text="def patch(): return True",
                reviewer_text="PASS",
                reviewer_verdict="PASS",
            )
        )

    client = TestClient(app)
    response = client.get(f"/debates/{debate_id}/stream", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    assert "event: round" in response.text
    assert "event: session_complete" in response.text
    assert "patch" in response.text
