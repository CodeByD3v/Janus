"""
worker.py — queue consumer that runs debates asynchronously.

Polls the database for queued DebateSession rows, claims them atomically
(no double-processing across parallel workers), runs the debate, and
writes results back.

Multiple worker processes can run in parallel safely — each uses
claim_queued_session() which does an atomic UPDATE with a lock to prevent
two workers from grabbing the same session.

Usage:
    python worker.py

Configuration via env vars (through config.py):
    WORKER_POLL_INTERVAL — seconds between poll cycles (default 5)
    WORKER_MAX_CONCURRENT — max concurrent debates per worker (default 4)
"""

from __future__ import annotations

import asyncio
import signal
import sys
import uuid
from datetime import datetime, timezone

from core.config import settings
from core.observability import get_logger
from storage.db import claim_queued_session, get_session, run_migrations
from storage.models import DebateSession

logger = get_logger(__name__)


class Worker:
    """Database-polling worker that runs adversarial code review debates.

    Design:
    - Polls the DB for queued sessions at a configurable interval
    - Claims sessions atomically via claim_queued_session()
    - Runs up to WORKER_MAX_CONCURRENT debates concurrently
    - Handles SIGINT/SIGTERM for graceful shutdown
    - Each debate gets its own sandbox and agent instances
    """

    def __init__(self) -> None:
        self.worker_id = f"worker-{uuid.uuid4().hex[:8]}"
        self.running = True
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._semaphore = asyncio.Semaphore(settings.WORKER_MAX_CONCURRENT)

    async def start(self) -> None:
        """Main worker loop. Polls for queued sessions and dispatches debates."""
        settings.validate_for_worker()
        run_migrations()

        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            poll_interval=settings.WORKER_POLL_INTERVAL,
            max_concurrent=settings.WORKER_MAX_CONCURRENT,
        )

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_shutdown)

        while self.running:
            try:
                await self._poll_cycle()
            except Exception:
                logger.error("worker_poll_error", exc_info=True)

            # Clean up completed tasks
            done = {t for t in self._active_tasks if t.done()}
            for t in done:
                self._active_tasks.discard(t)
                if t.exception():
                    logger.error(
                        "debate_task_failed",
                        error=str(t.exception()),
                    )

            await asyncio.sleep(settings.WORKER_POLL_INTERVAL)

        # Wait for active debates to finish on shutdown
        if self._active_tasks:
            logger.info(
                "worker_draining",
                active_debates=len(self._active_tasks),
            )
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

        logger.info("worker_stopped", worker_id=self.worker_id)

    async def _poll_cycle(self) -> None:
        """Try to claim and start one debate."""
        if not self._semaphore._value:  # type: ignore[attr-defined]
            return  # At max concurrency, skip this cycle

        session_id = claim_queued_session(self.worker_id)
        if session_id is None:
            return  # No queued sessions

        logger.info(
            "debate_claimed",
            worker_id=self.worker_id,
            debate_id=session_id,
        )

        task = asyncio.create_task(self._run_debate(session_id))
        self._active_tasks.add(task)

    async def _run_debate(self, session_id: str) -> None:
        """Run a single debate, guarded by the concurrency semaphore."""
        async with self._semaphore:
            # Load session details from DB
            with get_session() as db:
                session = db.query(DebateSession).filter_by(id=session_id).first()
                if session is None:
                    logger.error("debate_session_not_found", debate_id=session_id)
                    return

                repo_ref = session.repo_ref
                target_file = session.target_file
                ticket = session.ticket
                tenant_id = session.tenant_id
                pr_repo = session.pr_repo
                pr_number = session.pr_number
                webhook_url = session.webhook_url

            logger.info(
                "debate_running",
                debate_id=session_id,
                repo_ref=repo_ref,
                target_file=target_file,
            )

            try:
                # Import here to avoid circular imports at module level
                from core.orchestrator import run_debate

                result = await run_debate(
                    repo_dir=repo_ref,
                    target_file=target_file,
                    ticket=ticket,
                    debate_id=session_id,
                    tenant_id=tenant_id,
                )

                logger.info(
                    "debate_completed_by_worker",
                    worker_id=self.worker_id,
                    debate_id=session_id,
                    merged=result.merged,
                    rounds=len(result.rounds),
                )

                # GAP 17 / TASK 18: optional side effects, fired only if a
                # PR reference and/or webhook was set on this session — a
                # no-op otherwise. Failures here are logged and swallowed
                # inside notify_debate_outcome(); they must never affect
                # the already-completed, already-persisted debate result.
                from dataclasses import asdict

                from core.notifications import notify_debate_outcome

                notify_debate_outcome(
                    debate_id=session_id,
                    merged=result.merged,
                    rounds=[asdict(r) for r in result.rounds],
                    final_gate=result.final_gate,
                    pr_repo=pr_repo,
                    pr_number=pr_number,
                    webhook_url=webhook_url,
                )

            except Exception as e:
                logger.error(
                    "debate_failed",
                    debate_id=session_id,
                    error=str(e),
                    exc_info=True,
                )
                # Mark session as errored
                with get_session() as db:
                    session = db.query(DebateSession).filter_by(id=session_id).first()
                    if session:
                        session.status = "error"  # type: ignore[assignment]
                        session.error_message = str(e)  # type: ignore[assignment]
                        session.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]

    def _handle_shutdown(self) -> None:
        """Handle SIGINT/SIGTERM for graceful shutdown."""
        logger.info("worker_shutdown_requested", worker_id=self.worker_id)
        self.running = False


def main() -> None:
    """Entry point for the worker process."""
    worker = Worker()
    try:
        asyncio.run(worker.start())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")


if __name__ == "__main__":
    main()
