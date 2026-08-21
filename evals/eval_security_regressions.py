"""Regression tests for the remaining audit findings closed in this pass."""

from __future__ import annotations

import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings
from core.gate import run_full_gate


def test_custom_checks_reject_non_sandbox_paths():
    outside = Path.cwd() / "_janus_custom_check_probe"
    outside.mkdir(exist_ok=True)
    try:
        (outside / "janus.yaml").write_text(
            "validation:\n  checks:\n    - name: probe\n      command: python -c pass\n"
        )
        result = run_full_gate(str(outside))
        assert result["passed"] is False
        assert result["checks"][0]["check"] == "sandbox_path"
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_mcp_server_default_path_exists():
    assert Path(settings.MCP_SERVER_SCRIPT).is_file()
