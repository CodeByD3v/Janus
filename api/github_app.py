"""
api/github_app.py — Phase 6 GitHub App integration.
Handles webhooks from GitHub to trigger adversarial code review.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from core.config import settings
from core.observability import get_logger
from storage.db import get_session
from storage.models import DebateSession

logger = get_logger(__name__)

github_router = APIRouter(prefix="/github", tags=["github"])

SUPPORTED_EXTENSIONS = {".py", ".js", ".ts", ".go", ".java", ".cpp", ".c", ".rs"}


def _verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _get_primary_target_file(pr_repo: str, pr_number: int) -> str | None:
    """Fetch changed files from the PR and pick a primary target."""
    token = settings.GITHUB_TOKEN
    if not token:
        return None
    url = f"{settings.GITHUB_API_URL}/repos/{pr_repo}/pulls/{pr_number}/files"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code >= 300:
            logger.warning("github_api_error_fetching_files", status_code=resp.status_code, body=resp.text[:200])
            return None
        
        files = resp.json()
        for f in files:
            filename = f.get("filename", "")
            if any(filename.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                return filename
        
        if files:
            return files[0].get("filename")
    except Exception as e:
        logger.warning("github_api_exception", error=str(e))
    return None


def _post_ack_comment(pr_repo: str, pr_number: int) -> None:
    """Post an acknowledgment comment on the PR."""
    token = settings.GITHUB_TOKEN
    if not token:
        return
    url = f"{settings.GITHUB_API_URL}/repos/{pr_repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(url, headers=headers, json={"body": "🔍 Janus review started..."})
    except Exception as e:
        logger.warning("github_api_exception_posting_comment", error=str(e))


@github_router.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(None),
    x_hub_signature_256: str = Header(None),
):
    """Handle incoming GitHub webhooks."""
    payload = await request.body()
    secret = settings.GITHUB_WEBHOOK_SECRET
    
    if secret:
        if not x_hub_signature_256:
            raise HTTPException(status_code=401, detail="Missing signature")
        if not _verify_webhook_signature(payload, x_hub_signature_256, secret):
            logger.warning("invalid_github_webhook_signature")
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = await request.json()
    action = data.get("action")
    
    # Extract PR info based on event type
    pr_repo = None
    pr_number = None
    commit_sha = None
    ticket_body = ""
    head_repo = None
    base_repo = None
    
    if x_github_event == "issue_comment" and action == "created":
        comment_body = data.get("comment", {}).get("body", "")
        if "/janus review" not in comment_body:
            return {"status": "ignored"}
        issue = data.get("issue", {})
        if "pull_request" not in issue:
            return {"status": "ignored"}
            
        pr_repo = data.get("repository", {}).get("full_name")
        pr_number = issue.get("number")
        ticket_body = "Triggered by comment: " + comment_body
        
        # We need the PR head info to check fork
        token = settings.GITHUB_TOKEN
        if token and pr_repo and pr_number:
            url = f"{settings.GITHUB_API_URL}/repos/{pr_repo}/pulls/{pr_number}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(url, headers=headers)
                if resp.status_code < 300:
                    pr_data = resp.json()
                    head_repo = pr_data.get("head", {}).get("repo", {}).get("full_name")
                    base_repo = pr_data.get("base", {}).get("repo", {}).get("full_name")
                    commit_sha = pr_data.get("head", {}).get("sha")
            except Exception as e:
                logger.warning("github_api_exception", error=str(e))
                
    elif x_github_event == "pull_request" and action in ("opened", "synchronize"):
        pr = data.get("pull_request", {})
        # Note: in a real implementation we would check `trigger: automatic` in janus.yaml here
        pr_repo = data.get("repository", {}).get("full_name")
        pr_number = pr.get("number")
        commit_sha = pr.get("head", {}).get("sha")
        ticket_body = pr.get("title", "") + "\n" + (pr.get("body") or "")
        head_repo = pr.get("head", {}).get("repo", {}).get("full_name")
        base_repo = pr.get("base", {}).get("repo", {}).get("full_name")
        
    else:
        return {"status": "ignored"}

    if not pr_repo or not pr_number:
        return {"status": "ignored"}

    # Hard Rule 10: Fork PR protection
    if head_repo and base_repo and head_repo != base_repo:
        logger.warning("fork_pr_ignored", pr_repo=pr_repo, pr_number=pr_number, head=head_repo)
        return {"status": "fork_pr_ignored"}

    # Extract target file
    target_file = _get_primary_target_file(pr_repo, pr_number)
    if not target_file:
        target_file = "unknown"

    debate_id = str(uuid.uuid4())
    
    # Enqueue debate
    with get_session() as db:
        session = DebateSession(
            id=debate_id,
            repo_ref=pr_repo,
            target_file=target_file,
            ticket=ticket_body,
            status="queued",
            pr_repo=pr_repo,
            pr_number=pr_number,
            commit_sha=commit_sha,
        )
        db.add(session)
        
    logger.info("github_webhook_debate_enqueued", debate_id=debate_id, pr_repo=pr_repo, pr_number=pr_number)
    
    _post_ack_comment(pr_repo, pr_number)
    
    return {"status": "review_queued", "debate_id": debate_id}
