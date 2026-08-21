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
from pathlib import Path
from datetime import datetime, timezone

from core.config import ModelConfig, settings
from core.observability import get_logger
from storage.db import claim_queued_session, get_session, run_migrations, sweep_zombie_sessions
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
        self._last_sweep_at: datetime | None = None

    async def start(self) -> None:
        """Main worker loop. Polls for queued sessions and dispatches debates."""
        settings.validate_for_worker()
        run_migrations()
        from core.retrieval import initialize_store
        initialize_store()

        logger.info(
            "worker_started",
            worker_id=self.worker_id,
            poll_interval=settings.WORKER_POLL_INTERVAL,
            max_concurrent=settings.WORKER_MAX_CONCURRENT,
            zombie_timeout_minutes=settings.ZOMBIE_SESSION_TIMEOUT_MINUTES,
            zombie_sweep_interval_seconds=settings.ZOMBIE_SWEEP_INTERVAL_SECONDS,
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

            self._maybe_sweep_zombies()

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

    def _maybe_sweep_zombies(self) -> None:
        """Run the zombie-session sweep if ZOMBIE_SWEEP_INTERVAL_SECONDS
        has elapsed since the last sweep (or this is the first cycle —
        _last_sweep_at starts as None, so a worker that just restarted
        after a crash cleans up any zombies from the PREVIOUS crash
        immediately, rather than waiting a full interval first).

        Deliberately synchronous (not awaited/run in an executor) — this
        is a fast, infrequent DB query, not worth the complexity of
        offloading from the event loop, and every other call in this
        poll cycle (claim_queued_session, get_session) is already
        synchronous DB access called directly from this same async
        function.
        """
        now = datetime.now(timezone.utc)
        if (
            self._last_sweep_at is not None
            and (now - self._last_sweep_at).total_seconds()
            < settings.ZOMBIE_SWEEP_INTERVAL_SECONDS
        ):
            return

        try:
            sweep_zombie_sessions(settings.ZOMBIE_SESSION_TIMEOUT_MINUTES)
        except Exception:
            logger.error("zombie_sweep_error", exc_info=True)
        finally:
            self._last_sweep_at = now

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
                pr_branch = session.pr_branch
                pr_author = session.pr_author
                webhook_url = session.webhook_url
                model_provider = session.model_provider
                model_name = session.model_name
                model_api_key_encrypted = session.model_api_key_encrypted

            logger.info(
                "debate_running",
                debate_id=session_id,
                repo_ref=repo_ref,
                target_file=target_file,
            )

            materialized_repo: Path | None = None
            effective_repo_ref = repo_ref
            try:
                # GitHub webhook rows carry a slug, not a local filesystem path.
                # Materialize the exact reviewed commit before sandbox_copy().
                if pr_repo and pr_number and not Path(repo_ref).is_dir():
                    if not session.commit_sha:
                        raise RuntimeError("GitHub review is missing a commit SHA")
                    from core.github_materializer import materialize_github_repo
                    materialized_repo = materialize_github_repo(pr_repo, session.commit_sha)
                    effective_repo_ref = str(materialized_repo)

                # Import here to avoid circular imports at module level
                from core.orchestrator import run_debate
                from core.repo_config import load_repo_config

                repo_config = load_repo_config(effective_repo_ref)
                model_config = None
                if model_provider and model_name:
                    model_api_key = ""
                    if model_api_key_encrypted:
                        from core.credentials import decrypt_secret
                        model_api_key = decrypt_secret(model_api_key_encrypted)
                    model_config = ModelConfig(
                        provider=model_provider,
                        model=model_name,
                        api_key=model_api_key,
                    )
                else:
                    model_config = repo_config.to_model_config()

                result = await run_debate(
                    repo_dir=effective_repo_ref,
                    target_file=target_file,
                    ticket=ticket,
                    debate_id=session_id,
                    tenant_id=tenant_id,
                    model_config=model_config,
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

                # Phase 7: Auto-merge if all conditions are met.
                # Best-effort — a failed merge never retroactively fails
                # the debate. Only attempted when there's a PR to merge.
                if pr_repo and pr_number and result.merged:
                    try:
                        from core.auto_merge import should_auto_merge, execute_auto_merge
                        from core.repo_config import load_repo_config

                        repo_config = load_repo_config(effective_repo_ref)
                        if should_auto_merge(
                            repo_config=repo_config,
                            merged=result.merged,
                            needs_human_review=result.needs_human_review,
                            pr_branch=pr_branch,
                            pr_author=pr_author,
                        ):
                            # Load commit_sha from session for SHA pinning
                            with get_session() as db:
                                s = db.query(DebateSession).filter_by(id=session_id).first()
                                sha = s.commit_sha if s else None
                            execute_auto_merge(
                                pr_repo=pr_repo,
                                pr_number=pr_number,
                                commit_sha=sha,
                            )
                    except Exception as e:
                        logger.warning(
                            "auto_merge_error",
                            debate_id=session_id,
                            error=str(e),
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
            finally:
                if materialized_repo is not None:
                    from core.github_materializer import cleanup_materialized_repo
                    cleanup_materialized_repo(materialized_repo)

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
