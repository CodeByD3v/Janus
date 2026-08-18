"""repo_config.py — per-repo configuration via janus.yaml."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class CheckConfig:
    """A single validation check from janus.yaml."""
    name: str
    command: str       # Shell command, e.g. "ruff check ."
    timeout: int = 60  # Seconds

@dataclass
class RepoConfig:
    """Parsed janus.yaml. All fields have sensible defaults."""
    checks: list[CheckConfig] = field(default_factory=list)
    language: str = ""  # Auto-detected if empty (Phase 3)
    # Phase 7 fields — not used yet, but reserved in the schema:
    auto_merge: bool = False
    trigger: str = "manual"  # "manual" | "automatic"

    @classmethod
    def from_yaml(cls, path: Path) -> RepoConfig:
        """Load from a janus.yaml file. Returns empty config on any error."""
        try:
            with open(path) as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception:
            return cls()

        checks = []
        for c in raw.get("validation", {}).get("checks", []):
            if isinstance(c, dict) and "name" in c and "command" in c:
                checks.append(CheckConfig(
                    name=c["name"],
                    command=c["command"],
                    timeout=int(c.get("timeout", 60)),
                ))

        return cls(
            checks=checks,
            language=raw.get("language", ""),
            auto_merge=bool(raw.get("auto_merge", False)),
            trigger=raw.get("trigger", "manual"),
        )

def load_repo_config(repo_dir: str) -> RepoConfig:
    """Load janus.yaml from a repo, or return defaults."""
    path = Path(repo_dir) / "janus.yaml"
    if path.exists():
        return RepoConfig.from_yaml(path)
    return RepoConfig()
