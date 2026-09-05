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
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import ModelConfig, settings
from core.observability import get_logger
from storage.db import claim_queued_session, get_session, run_migrations, sweep_zombie_sessions
from storage.models import DebateSession

logger = get_logger(__name__)


def _load_session_details(session_id: str) -> dict[str, Any] | None:
    """Read immutable debate inputs in a worker thread."""
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=session_id).first()
        if session is None:
            return None
        return {
            "repo_ref": session.repo_ref,
            "target_file": session.target_file,
            "ticket": session.ticket,
            "tenant_id": session.tenant_id,
            "pr_repo": session.pr_repo,
            "pr_number": session.pr_number,
            "pr_branch": session.pr_branch,
            "pr_author": session.pr_author,
            "commit_sha": session.commit_sha,
            "github_installation_id": session.github_installation_id,
            "webhook_url": session.webhook_url,
            "model_provider": session.model_provider,
            "model_name": session.model_name,
            "model_api_key_encrypted": session.model_api_key_encrypted,
        }


def _mark_session_error(session_id: str, error_message: str) -> None:
    """Persist an execution error without running DB work on the event loop."""
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=session_id).first()
        if session:
            session.status = "error"  # type: ignore[assignment]
            session.error_message = error_message  # type: ignore[assignment]
            session.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]


def _load_commit_sha(session_id: str) -> str | None:
    """Load the reviewed commit SHA in a worker thread."""
    with get_session() as db:
        session = db.query(DebateSession).filter_by(id=session_id).first()
        return session.commit_sha if session else None


def _load_swept_pr_sessions(sweep_interval_seconds: int) -> list[dict[str, Any]]:
    """Load recently-swept sessions that have PR metadata for notifications.

    Returns sessions whose error_message starts with 'Swept by zombie'
    and were updated within the last sweep interval, so notifications
    fire exactly once per sweep cycle.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=sweep_interval_seconds + 30)
    results = []
    with get_session() as db:
        sessions = (
            db.query(DebateSession)
            .filter(
                DebateSession.status == "error",
                DebateSession.pr_repo.isnot(None),
                DebateSession.pr_number.isnot(None),
                DebateSession.error_message.like("Swept by zombie%"),
                DebateSession.updated_at >= cutoff,
            )
            .all()
        )
        for s in sessions:
            results.append({
                "id": s.id,
                "pr_repo": s.pr_repo,
                "pr_number": s.pr_number,
                "commit_sha": s.commit_sha,
                "github_installation_id": s.github_installation_id,
                "tenant_id": s.tenant_id,
            })
    return results


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
        # Resolve heartbeat path: use the configured path, but on Windows
        # fall back to a tempdir-based path since /tmp doesn't exist.
        configured = settings.WORKER_HEARTBEAT_FILE
        if sys.platform == "win32" and configured.startswith("/tmp/"):
            configured = str(
                Path(tempfile.gettempdir()) / configured.split("/tmp/", 1)[1]
            )
        self._heartbeat_path = Path(configured)

    def _touch_heartbeat(self) -> None:
        """Touch the heartbeat file to prove the worker's poll loop is alive.

        This is the signal Docker healthcheck and k8s livenessProbe read.
        If the worker's event loop stalls (the exact failure mode observed
        in the live test), the file's mtime goes stale, and the
        orchestrator restarts the container.
        """
        try:
            self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self._heartbeat_path.touch()
        except OSError:
            # Non-fatal: heartbeat is a liveness signal, not critical data.
            logger.warning("worker_heartbeat_touch_failed", path=str(self._heartbeat_path))

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

        # Register signal handlers (on Windows, add_signal_handler is not implemented)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_shutdown)
            except NotImplementedError:
                logger.info("worker_signal_handlers_unavailable", platform="windows")
                break

        while self.running:
            try:
                await self._poll_cycle()
            except Exception:
                logger.error("worker_poll_error", exc_info=True)

            await self._maybe_sweep_zombies()

            # Clean up completed tasks
            done = {t for t in self._active_tasks if t.done()}
            for t in done:
                self._active_tasks.discard(t)
                if t.exception():
                    logger.error(
                        "debate_task_failed",
                        error=str(t.exception()),
                    )

            # Touch heartbeat file to prove the poll loop is alive.
            self._touch_heartbeat()

            await asyncio.sleep(settings.WORKER_POLL_INTERVAL)

        # Wait for active debates to finish on shutdown
        if self._active_tasks:
            logger.info(
                "worker_draining",
                active_debates=len(self._active_tasks),
            )
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

        logger.info("worker_stopped", worker_id=self.worker_id)

    async def _maybe_sweep_zombies(self) -> None:
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
            result = await asyncio.to_thread(
                sweep_zombie_sessions,
                settings.ZOMBIE_SESSION_TIMEOUT_MINUTES,
                queued_timeout_minutes=settings.QUEUED_SESSION_TIMEOUT_MINUTES,
            )
            total = result["swept_running"] + result["swept_queued"]
            if total > 0:
                # Fire timeout notifications for swept sessions that
                # have GitHub PR metadata, so developers see "Janus
                # review timed out" instead of a perpetual 'pending'.
                await self._notify_swept_sessions()
        except Exception:
            logger.error("zombie_sweep_error", exc_info=True)
        finally:
            self._last_sweep_at = now

    async def _notify_swept_sessions(self) -> None:
        """Post timeout notifications for recently-swept sessions.

        Finds sessions whose error_message starts with 'Swept by zombie'
        that have PR metadata, and posts a comment + failure status on
        the PR so the developer knows the review timed out.  Only fires
        for sessions swept in the current sweep (updated_at within the
        last sweep interval).
        """
        try:
            swept_sessions = await asyncio.to_thread(
                _load_swept_pr_sessions,
                settings.ZOMBIE_SWEEP_INTERVAL_SECONDS,
            )
            for s in swept_sessions:
                logger.info(
                    "notifying_swept_session",
                    debate_id=s["id"],
                    pr_repo=s["pr_repo"],
                    pr_number=s["pr_number"],
                )
                from core.notifications import post_github_pr_comment

                post_github_pr_comment(
                    pr_repo=s["pr_repo"],
                    pr_number=s["pr_number"],
                    body=(
                        "⏱️ **Janus review timed out.**\n\n"
                        f"Debate `{s['id']}` did not complete within the "
                        f"configured timeout. This is usually caused by a "
                        f"worker crash or an API rate limit.\n\n"
                        f"Please re-trigger the review with `@janus review` "
                        f"or `/janus review`."
                    ),
                    installation_id=s.get("github_installation_id"),
                    tenant_id=s.get("tenant_id"),
                )
                # Also update the commit status from 'pending' to 'error'
                if s.get("commit_sha"):
                    try:
                        from api.github_app import post_commit_status

                        post_commit_status(
                            pr_repo=s["pr_repo"],
                            commit_sha=s["commit_sha"],
                            state="error",
                            description="Janus review timed out",
                            installation_id=s.get("github_installation_id"),
                            tenant_id=s.get("tenant_id"),
                        )
                    except Exception as exc:
                        logger.warning(
                            "swept_session_status_update_failed",
                            debate_id=s["id"],
                            error=str(exc),
                        )
        except Exception:
            logger.warning("swept_session_notifications_failed", exc_info=True)

    async def _poll_cycle(self) -> None:
        """Try to claim and start one debate."""
        if not self._semaphore._value:  # type: ignore[attr-defined]
            return  # At max concurrency, skip this cycle

        session_id = await asyncio.to_thread(claim_queued_session, self.worker_id)
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
            # Load immutable session details off the event loop.
            session_data = await asyncio.to_thread(_load_session_details, session_id)
            if session_data is None:
                logger.error("debate_session_not_found", debate_id=session_id)
                return

            repo_ref = session_data["repo_ref"]
            target_file = session_data["target_file"]
            ticket = session_data["ticket"]
            tenant_id = session_data["tenant_id"]
            pr_repo = session_data["pr_repo"]
            pr_number = session_data["pr_number"]
            pr_branch = session_data["pr_branch"]
            pr_author = session_data["pr_author"]
            commit_sha = session_data["commit_sha"]
            github_installation_id = session_data["github_installation_id"]
            webhook_url = session_data["webhook_url"]
            model_provider = session_data["model_provider"]
            model_name = session_data["model_name"]
            model_api_key_encrypted = session_data["model_api_key_encrypted"]

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
                    if not commit_sha:
                        raise RuntimeError("GitHub review is missing a commit SHA")
                    from core.github_materializer import materialize_github_repo
                    materialized_repo = materialize_github_repo(
                        pr_repo,
                        commit_sha,
                        installation_id=github_installation_id,
                        tenant_id=tenant_id,
                    )
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
                    installation_id=github_installation_id,
                    tenant_id=tenant_id,
                    commit_sha=commit_sha,
                    needs_human_review=result.needs_human_review,
                )

                # Phase 7: Auto-merge if all conditions are met.
                # Best-effort — a failed merge never retroactively fails
                # the debate. Only attempted when there's a PR to merge.
                if pr_repo and pr_number and result.merged:
                    try:
                        from core.auto_merge import execute_auto_merge, should_auto_merge
                        from core.repo_config import load_repo_config

                        repo_config = load_repo_config(effective_repo_ref)
                        if should_auto_merge(
                            repo_config=repo_config,
                            merged=result.merged,
                            needs_human_review=result.needs_human_review,
                            pr_branch=pr_branch,
                            pr_author=pr_author,
                        ):
                            # Load commit_sha from session for SHA pinning off-loop.
                            sha = await asyncio.to_thread(_load_commit_sha, session_id)
                            execute_auto_merge(
                                pr_repo=pr_repo,
                                pr_number=pr_number,
                                commit_sha=sha,
                                installation_id=github_installation_id,
                                tenant_id=tenant_id,
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
                # Mark session as errored without blocking the event loop.
                await asyncio.to_thread(_mark_session_error, session_id, str(e))
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
