"""Prompt-rendering regressions for ADK agent construction."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.agents import _escape_adk_template_literals


def test_retrieved_code_braces_are_literal_adk_template_text() -> None:
    rendered = _escape_adk_template_literals("f\"SELECT * FROM users WHERE name = '{username}'\"")

    assert rendered == "f\"SELECT * FROM users WHERE name = '{\u200busername\u200b}'\""
