"""
auto_merge.py — Phase 7 enterprise auto-merge logic.

Auto-merges a PR when ALL of the following conditions are met:
1. The repo's janus.yaml has auto_merge: true
2. The Reviewer verdict is PASS (NOT INCONCLUSIVE — Hard Rule: never
   auto-merge if needs_human_review is True)
3. The gate passed (merged == True)
4. The PR branch matches an allowed pattern from auto_merge_branches
   (if any are configured — empty list means ALL branches are eligible)
5. The PR author matches auto_merge_authors (if any are configured —
   empty list means ALL authors are eligible)

The merge itself uses the GitHub API's merge endpoint, which requires
the GitHub App to have contents:write permission on the repository.

This module is best-effort: a failed merge attempt is logged but does
not retroactively fail the debate or gate outcome.
"""

from __future__ import annotations

import fnmatch
from typing import Any

import httpx

from core.config import settings
from core.observability import get_logger
from core.repo_config import RepoConfig

logger = get_logger(__name__)


def should_auto_merge(
    repo_config: RepoConfig,
    merged: bool,
    needs_human_review: bool,
    pr_branch: str | None = None,
    pr_author: str | None = None,
) -> bool:
    """Determine whether a debate outcome qualifies for auto-merge.

    Returns True only when every precondition is satisfied.
    Each check short-circuits — the first failure returns False.
    """
    # 1. Repo must explicitly opt in
    if not repo_config.auto_merge:
        return False

    # 2. Gate must have passed
    if not merged:
        return False

    # 3. Never auto-merge if human review is needed (INCONCLUSIVE verdict)
    if needs_human_review:
        logger.info(
            "auto_merge_blocked_human_review",
            reason="INCONCLUSIVE verdict requires human review",
        )
        return False

    # 4. Branch pattern check (empty list = all branches allowed)
    if repo_config.auto_merge_branches and pr_branch:
        if not any(
            fnmatch.fnmatch(pr_branch, pattern)
            for pattern in repo_config.auto_merge_branches
        ):
            logger.info(
                "auto_merge_blocked_branch",
                branch=pr_branch,
                allowed_patterns=repo_config.auto_merge_branches,
            )
            return False

    # 5. Author check (empty list = all authors allowed)
    if repo_config.auto_merge_authors and pr_author:
        if pr_author not in repo_config.auto_merge_authors:
            logger.info(
                "auto_merge_blocked_author",
                author=pr_author,
                allowed_authors=repo_config.auto_merge_authors,
            )
            return False

    return True


def execute_auto_merge(
    pr_repo: str,
    pr_number: int,
    commit_sha: str | None = None,
    merge_method: str = "squash",
) -> bool:
    """Merge a PR via the GitHub API. Returns True on success.

    merge_method: "merge", "squash", or "rebase" — squash is the
    safest default for automated merges (clean history).

    Best-effort: failures are logged, never raised. A failed auto-merge
    does not affect the debate outcome — the PR simply stays open for
    manual merge.
    """
    token = settings.GITHUB_TOKEN
    if not token:
        logger.warning("auto_merge_no_token", pr_repo=pr_repo, pr_number=pr_number)
        return False

    url = f"{settings.GITHUB_API_URL}/repos/{pr_repo}/pulls/{pr_number}/merge"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    body: dict[str, Any] = {
        "merge_method": merge_method,
        "commit_title": f"[Janus] Auto-merge PR #{pr_number}",
        "commit_message": (
            f"Janus adversarial code review passed.\n"
            f"Reviewer verdict: PASS\n"
            f"Gate: all checks passed"
        ),
    }
    if commit_sha:
        body["sha"] = commit_sha  # Ensures we merge the exact SHA we reviewed

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.put(url, headers=headers, json=body)

        if resp.status_code == 200:
            logger.info(
                "auto_merge_success",
                pr_repo=pr_repo,
                pr_number=pr_number,
                merge_method=merge_method,
            )
            return True
        elif resp.status_code == 405:
            # 405 = merge not allowed (branch protections, merge conflict, etc.)
            logger.warning(
                "auto_merge_not_allowed",
                pr_repo=pr_repo,
                pr_number=pr_number,
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
        elif resp.status_code == 409:
            # 409 = HEAD has been modified (commit SHA mismatch)
            logger.warning(
                "auto_merge_sha_mismatch",
                pr_repo=pr_repo,
                pr_number=pr_number,
            )
        else:
            logger.warning(
                "auto_merge_failed",
                pr_repo=pr_repo,
                pr_number=pr_number,
                status_code=resp.status_code,
                detail=resp.text[:300],
            )
    except Exception as e:
        logger.error(
            "auto_merge_exception",
            pr_repo=pr_repo,
            pr_number=pr_number,
            error=str(e),
        )

    return False
