"""Training-data boundary regressions; no model training or network access."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from training.dataset import export_sft_jsonl, load_review_examples


def _write_record(path: Path, **overrides: str) -> None:
    record = {
        "id": "example-1",
        "bug_pattern": "off_by_one",
        "code_snippet": "return values[0:page_size - 1]",
        "review_comment": "The slice drops the final requested item.",
        "fix_summary": "Use the exclusive endpoint page_size.",
        "provenance": {
            "repository": "example/repo",
            "pull_request": "42",
            "review_id": "review-1",
            "source_commit": "abc123",
            "fix_commit": "def456",
        },
        "executable_evidence": {
            "test_path": "tests/test_page.py::test_page_size",
            "test_command": "pytest tests/test_page.py::test_page_size",
            "failure_before_fix": True,
            "passes_after_fix": True,
        },
    }
    record.update(overrides)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_retrieval_seed_is_rejected_as_training_data(tmp_path):
    seed = Path(__file__).resolve().parent.parent / "data" / "real_catch_examples.seed.jsonl"

    with pytest.raises(ValueError, match="missing required fields"):
        load_review_examples(seed)


def test_valid_real_catch_record_exports_provider_neutral_record(tmp_path):
    source = tmp_path / "valid.jsonl"
    _write_record(source)
    examples = load_review_examples(source)
    output = tmp_path / "reviewer-sft.jsonl"
    count = export_sft_jsonl(examples, output)

    assert count == 1
    exported = [json.loads(line) for line in output.read_text().splitlines()]
    assert exported[0]["provenance"]["fix_commit"] == "def456"
    assert exported[0]["executable_evidence"]["failure_before_fix"] is True
    assert exported[0]["messages"][0]["role"] == "system"
    assert exported[0]["messages"][-1]["role"] == "assistant"


@pytest.mark.parametrize(
    "overrides",
    [
        {"review_comment": ""},
        {"fix_summary": ""},
        {"id": ""},
    ],
)
def test_dataset_rejects_empty_required_fields(tmp_path, overrides):
    source = tmp_path / "bad.jsonl"
    _write_record(source, **overrides)

    with pytest.raises(ValueError, match="empty or non-string fields"):
        load_review_examples(source)


def test_dataset_rejects_duplicate_ids(tmp_path):
    source = tmp_path / "duplicate.jsonl"
    _write_record(source)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(source.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="duplicate example id"):
        load_review_examples(source)
