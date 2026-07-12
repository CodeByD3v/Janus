"""
notifications.py — post debate outcomes where the developer already is
(GAP 17 / TASK 18).

Two optional, independent side effects fired after a debate completes:

- A GitHub PR comment, if `pr_repo` + `pr_number` were provided when the
  debate was enqueued (see api/schemas.py's CreateDebateRequest).
- A webhook POST with a JSON summary, if a `webhook_url` was provided
  per-request, or DEFAULT_WEBHOOK_URL is configured server-side as a
  fallback.

Neither requires a new UI — both are side effects at the end of a debate
that has ALREADY completed and been persisted. A debate with no PR
reference and no webhook configured (either per-request or via
DEFAULT_WEBHOOK_URL) behaves exactly as it did before this module
existed — notify_debate_outcome() is simply a no-op in that case.

WHY THIS USES A PR COMMENT, NOT A CHECK RUN: GitHub's Check Runs API
gives richer pass/fail UI in the PR's checks tab, but requires a GitHub
App installation token — meaningfully more setup (registering a GitHub
App, handling installation tokens) than the Issues API's comment
endpoint, which works with a plain personal access token. A Check Run
integration is a reasonable future upgrade (documented in AGENTS.md) if
richer UI is worth that setup cost; the comment endpoint satisfies "post
where the developer already is" without it.

Every function here is best-effort: failures are logged and swallowed,
never raised. A broken webhook or an expired GitHub token must not make
an already-successful, already-persisted debate look like it failed.
"""

from __future__ import annotations

from typing import Any

import requests

from core.config import settings
from core.observability import get_logger

logger = get_logger(__name__)

_SUMMARY_SNIPPET_CHARS = 300


def format_debate_summary(
    debate_id: str,
    merged: bool,
    rounds: list[dict[str, Any]],
    final_gate: dict[str, Any] | None,
) -> str:
    """Render a human-readable Markdown summary of a completed debate.

    `rounds` is a list of round dicts — either RoundLog converted via
    dataclasses.asdict(), or Round.to_dict() — both use the same key
    names, so this works with either the in-memory orchestrator result
    or a row reloaded from the database.
    """
    verdict = "Merged" if merged else "Rejected"
    lines: list[str] = [
        f"### Janus adversarial review — {verdict}",
        "",
        f"Debate `{debate_id}` ran {len(rounds)} round(s).",
        "",
    ]

    for r in rounds:
        round_num = r.get("round_num")
        reviewer_text = (r.get("reviewer_text") or "").strip()
        stop_reason = r.get("stop_reason")
        gate_result = r.get("gate_result") or {}
        gate_passed = gate_result.get("passed")
        gate_label = "pass" if gate_passed else "fail"

        header = f"**Round {round_num}** — gate: {gate_label}"
        if stop_reason:
            header += f" ({stop_reason})"
        lines.append(header)

        if reviewer_text:
            snippet = reviewer_text[:_SUMMARY_SNIPPET_CHARS]
            if len(reviewer_text) > _SUMMARY_SNIPPET_CHARS:
                snippet += "…"
            lines.append(f"> {snippet}")
        lines.append("")

    if final_gate:
        checks = final_gate.get("checks", [])
        if checks:
            lines.append("**Final gate:**")
            for c in checks:
                mark = "pass" if c.get("passed") else "FAIL"
                lines.append(f"- [{mark}] {c.get('check')}")

    return "\n".join(lines)


def post_github_pr_comment(pr_repo: str, pr_number: int, body: str) -> bool:
    """Post `body` as a comment on the given PR.

    Uses GitHub's Issues API (`/issues/{number}/comments`), which works
    for both issues and PRs — see this module's docstring for why a
    comment was chosen over a Check Run.

    Never raises. Returns True on success, False on any failure
    (missing token, network error, non-2xx response) — the caller does
    not need to handle exceptions from this function.
    """
    if not settings.GITHUB_TOKEN:
        logger.warning(
            "github_notification_skipped_no_token",
            pr_repo=pr_repo,
            pr_number=pr_number,
        )
        return False

    url = f"{settings.GITHUB_API_URL}/repos/{pr_repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        resp = requests.post(
            url,
            json={"body": body},
            headers=headers,
            timeout=settings.NOTIFICATION_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 300:
            logger.warning(
                "github_pr_comment_failed",
                pr_repo=pr_repo,
                pr_number=pr_number,
                status_code=resp.status_code,
                response=resp.text[:500],
            )
            return False

        logger.info("github_pr_comment_posted", pr_repo=pr_repo, pr_number=pr_number)
        return True

    except requests.RequestException as e:
        logger.warning(
            "github_pr_comment_error",
            pr_repo=pr_repo,
            pr_number=pr_number,
            error=str(e),
        )
        return False


def _is_safe_webhook_url(url: str) -> bool:
    import socket
    import ipaddress
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        
        # Resolve all IPs for the hostname
        addrinfo = socket.getaddrinfo(hostname, None)
        for info in addrinfo:
            ip_str = info[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (ip_obj.is_private or ip_obj.is_loopback or 
                ip_obj.is_link_local or ip_obj.is_multicast or 
                ip_obj.is_reserved or ip_obj.is_unspecified):
                return False
        return True
    except Exception:
        return False


def post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """POST a JSON summary payload to a configured webhook URL.

    Never raises. Returns True on success, False on any failure.
    """
    if not _is_safe_webhook_url(url):
        logger.warning("webhook_notification_rejected_ssrf", url=url)
        return False
        
    try:
        resp = requests.post(url, json=payload, timeout=settings.NOTIFICATION_TIMEOUT_SECONDS, allow_redirects=False)
        if resp.status_code >= 300:
            logger.warning(
                "webhook_notification_failed",
                url=url,
                status_code=resp.status_code,
            )
            return False

        logger.info("webhook_notification_sent", url=url)
        return True

    except requests.RequestException as e:
        logger.warning("webhook_notification_error", url=url, error=str(e))
        return False


def notify_debate_outcome(
    debate_id: str,
    merged: bool,
    rounds: list[dict[str, Any]],
    final_gate: dict[str, Any] | None,
    pr_repo: str | None = None,
    pr_number: int | None = None,
    webhook_url: str | None = None,
) -> None:
    """Fire both optional notification side effects for a completed debate.

    Both are independently optional:
    - Posts a PR comment only if BOTH pr_repo and pr_number are set
      (api/schemas.py's CreateDebateRequest already enforces they're
      provided together or not at all, so this mirrors that contract).
    - Posts a webhook only if webhook_url was passed, or
      settings.DEFAULT_WEBHOOK_URL is configured as a fallback.

    If neither is set, this function does nothing — a debate with no PR
    reference and no webhook configured is unaffected by this feature
    existing at all.
    """
    if not (pr_repo and pr_number) and not (webhook_url or settings.DEFAULT_WEBHOOK_URL):
        return

    summary = format_debate_summary(debate_id, merged, rounds, final_gate)

    if pr_repo and pr_number:
        post_github_pr_comment(pr_repo, pr_number, summary)

    effective_webhook = webhook_url or settings.DEFAULT_WEBHOOK_URL
    if effective_webhook:
        post_webhook(
            effective_webhook,
            {
                "debate_id": debate_id,
                "merged": merged,
                "round_count": len(rounds),
                "summary": summary,
            },
        )
