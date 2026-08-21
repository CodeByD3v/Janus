"""repo_config.py — per-repo configuration via janus.yaml."""

from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import ModelConfig

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
    test_patterns: list[str] = field(
        default_factory=lambda: ["tests", "testing", "test", "__tests__", "spec"]
    )
    # Phase 5 — per-repo model configuration (BYOK)
    model_provider: str = ""  # e.g. "openai", "anthropic"; empty = server default
    model_name: str = ""      # e.g. "gpt-4o", "claude-sonnet-4-20250514"; empty = server default
    # Phase 7 — auto-merge configuration
    auto_merge: bool = False  # Must be explicitly opted in
    trigger: str = "manual"  # "manual" (only /janus review) | "automatic" (on PR open/sync)
    auto_merge_branches: list[str] = field(default_factory=list)  # e.g. ["dependabot/*"]
    auto_merge_authors: list[str] = field(default_factory=list)  # Trusted authors for auto-merge

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

        tests_section = raw.get("tests", {})
        configured_test_patterns = (
            tests_section.get("directories", [])
            if isinstance(tests_section, dict)
            else []
        )
        test_patterns = raw.get("test_patterns", configured_test_patterns)
        if not isinstance(test_patterns, list) or not all(isinstance(item, str) for item in test_patterns):
            test_patterns = ["tests", "testing", "test", "__tests__", "spec"]

        # Model configuration from janus.yaml's 'model' section:
        #   model:
        #     provider: openai
        #     name: gpt-4o
        model_section = raw.get("model", {})
        if not isinstance(model_section, dict):
            model_section = {}

        # Auto-merge configuration from janus.yaml:
        #   auto_merge: true
        #   auto_merge_branches: ["dependabot/*"]
        #   auto_merge_authors: ["dependabot[bot]"]
        auto_merge_branches = raw.get("auto_merge_branches", [])
        if not isinstance(auto_merge_branches, list):
            auto_merge_branches = []
        auto_merge_authors = raw.get("auto_merge_authors", [])
        if not isinstance(auto_merge_authors, list):
            auto_merge_authors = []

        return cls(
            checks=checks,
            language=str(raw.get("language", "")),
            test_patterns=[str(item) for item in test_patterns],
            model_provider=str(model_section.get("provider", "")),
            model_name=model_section.get("name", ""),
            auto_merge=bool(raw.get("auto_merge", False)),
            trigger=raw.get("trigger", "manual"),
            auto_merge_branches=[str(b) for b in auto_merge_branches],
            auto_merge_authors=[str(a) for a in auto_merge_authors],
        )

    def to_model_config(self) -> "ModelConfig | None":
        """Convert the repo's model settings to a ModelConfig, or None
        if no model override is specified.

        API-request-level ModelConfig takes precedence over this —
        callers should check the request config first.
        """
        if not self.model_provider and not self.model_name:
            return None
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

