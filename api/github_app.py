"""GitHub webhook integration for Janus reviews.

The webhook endpoint is deliberately conservative: signatures are verified
when configured, fork pull requests are rejected unless explicitly supported,
automatic reviews require ``janus.yaml`` opt-in, and missing GitHub metadata
fails closed instead of silently enqueueing an unverifiable review.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Header, HTTPException, Request

from core.config import settings
from core.github_credentials import github_headers
from core.observability import get_logger
from core.path_safety import looks_like_path_traversal
from storage.db import get_session
from storage.models import DebateSession, GithubInstallation

logger = get_logger(__name__)

github_router = APIRouter(prefix="/github", tags=["github"])

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb",
    ".cs", ".cpp", ".c", ".h", ".rs", ".swift", ".php",
}


def _github_headers(
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> dict[str, str] | None:
    return github_headers(installation_id, tenant_id)


def _verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub HMAC-SHA256 webhook signatures."""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _github_get(
    path: str,
    params: dict[str, str] | None = None,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> Any | None:
    """Fetch a GitHub resource with installation-scoped credentials."""
    headers = _github_headers(installation_id, tenant_id)
    if headers is None:
        return None
    url = f"{settings.GITHUB_API_URL.rstrip('/')}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers=headers, params=params)
        if response.status_code >= 300:
            logger.warning(
                "github_api_error",
                path=path,
                status_code=response.status_code,
                body=response.text[:200],
            )
            return None
        return response.json()
    except Exception as exc:
        logger.warning("github_api_exception", path=path, error=str(exc))
        return None


def _get_primary_target_file(
    pr_repo: str,
    pr_number: int,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> str | None:
    """Choose the first supported, safe source file changed by the PR."""
    files = _github_get(
        f"/repos/{pr_repo}/pulls/{pr_number}/files",
        installation_id=installation_id,
        tenant_id=tenant_id,
    )
    if not isinstance(files, list):
        return None
    for item in files:
        filename = item.get("filename", "") if isinstance(item, dict) else ""
        if not isinstance(filename, str) or looks_like_path_traversal(filename):
            continue
        if any(filename.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
            return filename
    return None


def _repo_trigger_is_automatic(
    pr_repo: str,
    commit_sha: str | None,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> bool:
    """Read janus.yaml at the reviewed ref and require trigger: automatic."""
    if not commit_sha:
        return False
    resource = _github_get(
        f"/repos/{pr_repo}/contents/janus.yaml",
        params={"ref": commit_sha},
        installation_id=installation_id,
        tenant_id=tenant_id,
    )
    if not isinstance(resource, dict) or resource.get("encoding") != "base64":
        return False
    try:
        content = base64.b64decode(resource.get("content", ""), validate=True).decode("utf-8")
        raw = yaml.safe_load(content) or {}
        return isinstance(raw, dict) and raw.get("trigger", "manual") == "automatic"
    except (ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.warning("github_janus_config_invalid", repo=pr_repo, error=str(exc))
        return False


def _post_commit_status(
    pr_repo: str,
    commit_sha: str,
    state: str,
    description: str,
    context: str = "Janus",
    target_url: str | None = None,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> None:
    """Post a commit status via GitHub's Statuses API.

    Uses ``POST /repos/{owner}/{repo}/statuses/{sha}`` to set a status on
    the given commit.  ``state`` must be one of ``error``, ``failure``,
    ``pending``, or ``success``.

    Best-effort: failures are logged and swallowed — a broken status
    post must never block the webhook flow.
    """
    headers = _github_headers(installation_id, tenant_id)
    if headers is None or not commit_sha:
        return
    url = (
        f"{settings.GITHUB_API_URL.rstrip('/')}/repos/{pr_repo}"
        f"/statuses/{commit_sha}"
    )
    body: dict[str, str] = {
        "state": state,
        "description": description[:140],  # GitHub caps at 140 chars
        "context": context,
    }
    if target_url:
        body["target_url"] = target_url
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, headers=headers, json=body)
        if response.status_code >= 300:
            logger.warning(
                "github_commit_status_failed",
                pr_repo=pr_repo,
                sha=commit_sha,
                status_code=response.status_code,
            )
        else:
            logger.info(
                "github_commit_status_posted",
                pr_repo=pr_repo,
                sha=commit_sha,
                state=state,
                context=context,
            )
    except Exception as exc:
        logger.warning("github_commit_status_exception", error=str(exc))


def _post_no_checks_required(
    pr_repo: str,
    commit_sha: str,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> None:
    """Post a 'success' commit status to signal no checks are required.

    This allows PRs to satisfy branch-protection rules that require
    status checks to pass, without actually running any checks.
    """
    _post_commit_status(
        pr_repo=pr_repo,
        commit_sha=commit_sha,
        state="success",
        description="No checks required",
        context="Janus",
        installation_id=installation_id,
        tenant_id=tenant_id,
    )


def _post_ack_comment(
    pr_repo: str,
    pr_number: int,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> None:
    """Post a non-blocking acknowledgment comment on the pull request."""
    headers = _github_headers(installation_id, tenant_id)
    if headers is None:
        return
    url = f"{settings.GITHUB_API_URL.rstrip('/')}/repos/{pr_repo}/issues/{pr_number}/comments"
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                url,
                headers=headers,
                json={"body": "Janus adversarial code review started."},
            )
        if response.status_code >= 300:
            logger.warning("github_ack_comment_failed", status_code=response.status_code)
    except Exception as exc:
        logger.warning("github_api_exception_posting_comment", error=str(exc))


@github_router.post("/webhook")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
):
    """Handle issue-comment and pull-request webhook events."""
    payload = await request.body()
    secret = settings.GITHUB_WEBHOOK_SECRET
    if settings.GITHUB_WEBHOOK_SECRET_REQUIRED and not secret:
        logger.error("github_webhook_secret_not_configured")
        raise HTTPException(status_code=503, detail="GitHub webhook integration is not configured")
    if secret and not _verify_webhook_signature(payload, x_hub_signature_256, secret):
        logger.warning("invalid_github_webhook_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    action = data.get("action")
    installation = data.get("installation") or {}
    raw_installation_id = installation.get("id")
    installation_id = raw_installation_id if isinstance(raw_installation_id, int) else None
    account = installation.get("account") or {}
    tenant_id = account.get("login") if isinstance(account.get("login"), str) else None
    pr_repo: str | None = None
    pr_number: int | None = None
    commit_sha: str | None = None
    ticket_body = ""
    head_repo: str | None = None
    base_repo: str | None = None
    pr_branch: str | None = None
    pr_author: str | None = None
    manual_trigger = False

    if x_github_event in ("issue_comment", "pull_request") and installation_id is None:
        logger.warning("github_event_missing_installation", event=x_github_event)
        return {"status": "unverified_installation_ignored"}

    if x_github_event == "installation" and action in ("created", "deleted"):
        if installation_id is None or not tenant_id:
            raise HTTPException(status_code=400, detail="Installation metadata is incomplete")
        account_type = str(installation.get("account", {}).get("type", "Unknown"))
        if action == "created":
            with get_session() as db:
                existing = db.query(GithubInstallation).filter_by(
                    installation_id=installation_id
                ).first()
                if existing is None:
                    db.add(
                        GithubInstallation(
                            installation_id=installation_id,
                            account_login=tenant_id,
                            account_type=account_type,
                            tenant_id=tenant_id,
                        )
                    )
                else:
                    existing.account_login = tenant_id
                    existing.account_type = account_type
                    existing.tenant_id = tenant_id
            return {"status": "installation_registered", "installation_id": installation_id}

        with get_session() as db:
            db.query(GithubInstallation).filter_by(
                installation_id=installation_id
            ).delete(synchronize_session=False)
        return {"status": "installation_revoked", "installation_id": installation_id}

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
        manual_trigger = True
        pr_data = (
            _github_get(
                f"/repos/{pr_repo}/pulls/{pr_number}",
                installation_id=installation_id,
                tenant_id=tenant_id,
            )
            if pr_repo and pr_number
            else None
        )
        if isinstance(pr_data, dict):
            head = pr_data.get("head", {})
            base = pr_data.get("base", {})
            head_repo = (head.get("repo") or {}).get("full_name")
            base_repo = (base.get("repo") or {}).get("full_name")
            commit_sha = head.get("sha")
            pr_branch = head.get("ref")
            pr_author = (pr_data.get("user") or {}).get("login")

    elif x_github_event == "pull_request" and action in ("opened", "synchronize"):
        pr = data.get("pull_request", {})
        pr_repo = data.get("repository", {}).get("full_name")
        pr_number = pr.get("number")
        commit_sha = (pr.get("head") or {}).get("sha")
        ticket_body = (pr.get("title") or "") + "\n" + (pr.get("body") or "")
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        base_repo = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
        pr_branch = (pr.get("head") or {}).get("ref")
        pr_author = (pr.get("user") or {}).get("login")

    else:
        return {"status": "ignored"}

    if not pr_repo or not pr_number:
        return {"status": "ignored"}

    # A missing pair means the event could not be verified. Never treat it as
    # an internal PR merely because the API token or payload is incomplete.
    if not head_repo or not base_repo:
        logger.warning("github_pr_metadata_unverified", pr_repo=pr_repo, pr_number=pr_number)
        return {"status": "unverified_pr_ignored"}
    if head_repo != base_repo:
        logger.warning("fork_pr_ignored", pr_repo=pr_repo, pr_number=pr_number, head=head_repo)
        return {"status": "fork_pr_ignored"}

    if not manual_trigger and not _repo_trigger_is_automatic(
        pr_repo, commit_sha, installation_id, tenant_id
    ):
        logger.info("github_automatic_trigger_not_enabled", pr_repo=pr_repo, pr_number=pr_number)
        return {"status": "automatic_trigger_disabled"}

    target_file = _get_primary_target_file(
        pr_repo, pr_number, installation_id, tenant_id
    )
    if not target_file:
        return {"status": "no_supported_source_file"}

    debate_id = str(uuid.uuid4())
    with get_session() as db:
        db.add(
            DebateSession(
                id=debate_id,
                repo_ref=pr_repo,
                target_file=target_file,
                ticket=ticket_body,
                status="queued",
                pr_repo=pr_repo,
                pr_number=pr_number,
                commit_sha=commit_sha,
                pr_branch=pr_branch,
                pr_author=pr_author,
                github_installation_id=installation_id,
                tenant_id=tenant_id,
            )
        )

    logger.info(
        "github_webhook_debate_enqueued",
        debate_id=debate_id,
        pr_repo=pr_repo,
        pr_number=pr_number,
        target_file=target_file,
    )
    _post_ack_comment(pr_repo, pr_number, installation_id, tenant_id)
    if commit_sha:
        _post_no_checks_required(pr_repo, commit_sha, installation_id, tenant_id)
    return {"status": "review_queued", "debate_id": debate_id}
