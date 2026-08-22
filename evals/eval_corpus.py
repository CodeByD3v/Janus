"""Corpus-level repository-context regressions across supported source shapes."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.repo_context import retrieve_repo_context

ROOT = Path(__file__).resolve().parent / "corpus"


@pytest.mark.parametrize(
    ("case", "target", "caller"),
    [
        ("python_average", "module.py", "caller.py"),
        ("typescript_service", "service.ts", "consumer.ts"),
        ("go_service", "service.go", "consumer.go"),
    ],
)
def test_context_finds_callers_across_language_fixtures(case: str, target: str, caller: str):
    repo = ROOT / case
    current_code = (repo / target).read_text(encoding="utf-8")

    context = retrieve_repo_context(str(repo), target, current_code)

    assert caller in context["call_graph"]["callers"]
    assert context["test_conventions"]


@pytest.mark.parametrize(
    ("case", "target"),
    [
        ("python_average", "module.py"),
        ("typescript_service", "service.ts"),
        ("go_service", "service.go"),
    ],
)
def test_context_is_bounded_and_serializable(case: str, target: str):
    repo = ROOT / case
    context = retrieve_repo_context(str(repo), target, (repo / target).read_text())

    assert set(context) == {"call_graph", "prior_fixes", "test_conventions"}
    assert len(context["call_graph"]["callers"]) <= 200
    assert all(isinstance(item, str) for item in context["test_conventions"])
