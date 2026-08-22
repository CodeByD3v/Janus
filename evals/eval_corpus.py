"""Corpus-level repository-context regressions across supported source shapes."""

from __future__ import annotations

import json
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


def test_non_python_comments_and_strings_do_not_create_caller_edges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = "export function calculateTotal(items: number[]): number { return items.length; }\n"
    (repo / "service.ts").write_text(target, encoding="utf-8")
    (repo / "comment_only.ts").write_text(
        "// calculateTotal()\nconst example = 'calculateTotal()';\n",
        encoding="utf-8",
    )
    (repo / "real_consumer.ts").write_text(
        "export const report = () => calculateTotal([]);\n",
        encoding="utf-8",
    )

    context = retrieve_repo_context(str(repo), "service.ts", target)

    assert context["call_graph"]["callers"] == ["real_consumer.ts"]
    assert "comment_only.ts" not in context["call_graph"]["callers"]


def test_non_python_block_comments_and_backticks_do_not_create_caller_edges(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = "export function calculateTotal(items: number[]): number { return items.length; }\n"
    (repo / "service.ts").write_text(target, encoding="utf-8")
    (repo / "examples.ts").write_text(
        "/* calculateTotal() */\nconst example = `calculateTotal()`;\n",
        encoding="utf-8",
    )

    context = retrieve_repo_context(str(repo), "service.ts", target)

    assert context["call_graph"]["callers"] == []


def test_non_python_symbol_extraction_remains_fail_soft_for_unreadable_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = "export function calculateTotal(items: number[]): number { return items.length; }\n"
    (repo / "service.ts").write_text(target, encoding="utf-8")
    (repo / "binary.ts").write_bytes(b"\\xff\\xfecalculateTotal()")

    context = retrieve_repo_context(str(repo), "service.ts", target)

    assert context["call_graph"]["callers"] == []

def test_masked_non_python_source_preserves_valid_calls():
    from core.repo_context import _non_python_symbol_usage

    defined, called = _non_python_symbol_usage(
        "function calculateTotal() {}\n// calculateTotal()\nconst s = 'calculateTotal()';\ncalculateTotal();"
    )

    assert defined == {"calculateTotal"}
    assert called == {"calculateTotal"}



def test_manifest_evaluator_accepts_explicit_local_fixture(tmp_path):
    from core.corpus_eval import evaluate_corpus_manifest

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "service.ts").write_text(
        "export function calculateTotal(items: number[]): number { return items.length; }\n",
        encoding="utf-8",
    )
    (repo / "consumer.ts").write_text(
        "export const report = () => calculateTotal([]);\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "fixture",
                        "path": str(repo),
                        "targets": [
                            {
                                "file": "service.ts",
                                "expected_symbols": ["calculateTotal"],
                                "expected_callers": ["consumer.ts"],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_corpus_manifest(manifest)

    assert report["passed"] is True
    assert report["targets"] == 1
    assert report["passed_targets"] == 1


def test_manifest_evaluator_rejects_missing_repository(tmp_path):
    from core.corpus_eval import evaluate_corpus_manifest

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "missing",
                        "path": str(tmp_path / "does-not-exist"),
                        "targets": ["service.ts"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_corpus_manifest(manifest)

    assert report["passed"] is False
    assert report["failed_targets"] == 1
    assert report["results"][0]["passed"] is False
    assert "not a directory" in report["results"][0]["error"]
