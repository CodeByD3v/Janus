"""Materialize a GitHub repository commit into an isolated local directory."""

from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx

from core.config import settings
from core.github_credentials import github_headers
from core.observability import get_logger

logger = get_logger(__name__)


def _github_headers(
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> dict[str, str]:
    headers = github_headers(installation_id, tenant_id)
    if headers is None:
        raise RuntimeError("No GitHub credentials are configured")
    return headers


def _safe_member_path(root: Path, member_name: str) -> Path:
    candidate = (root / member_name).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("GitHub archive contains a path outside its extraction root")
    return candidate


def materialize_github_repo(
    repo_slug: str,
    commit_sha: str,
    installation_id: int | None = None,
    tenant_id: str | None = None,
) -> Path:
    """Download an exact GitHub commit archive and return its extracted root.

    Only regular files and directories are extracted. Symlinks and hardlinks
    are rejected to prevent archive entries from escaping the materialization
    root or redirecting later sandbox-copy operations.
    """
    if "/" not in repo_slug or not commit_sha:
        raise ValueError("A repository slug and commit SHA are required")

    cache_root = Path(settings.GITHUB_REPO_CACHE_DIR).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix="janus-github-", dir=cache_root))
    archive_path = work_root / "repo.tar.gz"
    extract_root = work_root / "extracted"
    extract_root.mkdir()

    url = f"{settings.GITHUB_API_URL.rstrip('/')}/repos/{repo_slug}/tarball/{commit_sha}"
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            response = client.get(
                url,
                headers=_github_headers(installation_id, tenant_id),
            )

        if response.status_code >= 300:
            raise RuntimeError(
                f"GitHub archive download failed with status {response.status_code}"
            )
        archive_path.write_bytes(response.content)

        with tarfile.open(archive_path, mode="r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                _safe_member_path(extract_root, member.name)
                if not (member.isdir() or member.isreg()):
                    raise ValueError("GitHub archive contains unsupported link or device entry")
            # Python 3.11 has no tarfile.extractall(filter=...). The
            # validated member walk above provides equivalent safety here.
            archive.extractall(extract_root, members=members)

        roots = [path for path in extract_root.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise ValueError("GitHub archive has an unexpected top-level layout")
        logger.info(
            "github_repo_materialized",
            repo=repo_slug,
            commit_sha=commit_sha,
            materialized_root=str(roots[0]),
        )
        return roots[0]
    except Exception:
        shutil.rmtree(work_root, ignore_errors=True)
        raise


def cleanup_materialized_repo(repo_path: Path) -> None:
    """Remove a materialized repository and its private temporary parent."""
    work_root = repo_path.resolve().parent.parent
    cache_root = Path(settings.GITHUB_REPO_CACHE_DIR).resolve()
    if work_root.is_relative_to(cache_root) and work_root != cache_root:
        shutil.rmtree(work_root, ignore_errors=True)
