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
    # Phase 5 — per-repo model configuration (BYOK)
    model_provider: str = ""  # e.g. "openai", "anthropic"; empty = server default
    model_name: str = ""      # e.g. "gpt-4o", "claude-sonnet-4-20250514"; empty = server default
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

        # Model configuration from janus.yaml's 'model' section:
        #   model:
        #     provider: openai
        #     name: gpt-4o
        model_section = raw.get("model", {})
        if not isinstance(model_section, dict):
            model_section = {}

        return cls(
            checks=checks,
            language=raw.get("language", ""),
            model_provider=model_section.get("provider", ""),
            model_name=model_section.get("name", ""),
            auto_merge=bool(raw.get("auto_merge", False)),
            trigger=raw.get("trigger", "manual"),
        )

    def to_model_config(self) -> "ModelConfig | None":
        """Convert the repo's model settings to a ModelConfig, or None
        if no model override is specified.

        API-request-level ModelConfig takes precedence over this —
        callers should check the request config first.
        """
        if not self.model_provider and not self.model_name:
            return None
        from core.config import ModelConfig
        return ModelConfig(
            provider=self.model_provider or "google",
            model=self.model_name,
        )

def load_repo_config(repo_dir: str) -> RepoConfig:
    """Load janus.yaml from a repo, or return defaults."""
    path = Path(repo_dir) / "janus.yaml"
    if path.exists():
        return RepoConfig.from_yaml(path)
    return RepoConfig()

