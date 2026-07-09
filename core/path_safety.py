"""
core/path_safety.py — Validation functions for filesystem paths.
"""
from pathlib import Path

def looks_like_path_traversal(path: str) -> bool:
    """Check if a path string contains parent traversal components."""
    if ".." in path:
        return True
    if path.startswith("/") or path.startswith("\\"):
        return True
    return False

def validate_repo_ref(repo_ref: str) -> None:
    """Validate that the repo_ref points to an allowed directory.
    
    Since this is a multi-tenant API, allowing arbitrary repo_ref like 
    '/' or 'C:\\' would be catastrophic. We must enforce a strict allowlist
    of repositories the service is allowed to review.
    """
    if looks_like_path_traversal(repo_ref):
        raise ValueError("repo_ref must not contain path traversals")
    
    # In a production system, this would query a database of tenant-owned
    # repositories. For the scope of this project, we restrict it to demo_repo
    # or specific test directories.
    allowed_repos = ["demo_repo", "./demo_repo", "demo_repo/"]
    
    # If it's an absolute path, it must be exactly the demo_repo in this project
    # which is often used in orchestrator.py tests via __file__.parent.parent / "demo_repo"
    project_root = Path(__file__).parent.parent
    demo_repo_abs = str(project_root / "demo_repo")
    
    if repo_ref not in allowed_repos and repo_ref != demo_repo_abs:
        raise ValueError(
            f"repo_ref '{repo_ref}' is not permitted. Only allowed repos can be reviewed."
        )
