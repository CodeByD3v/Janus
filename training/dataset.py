"""Validate and export real-catch Reviewer data without inventing examples.

This module deliberately stops before model-specific fine-tuning. It produces a
provider-neutral chat JSONL file that a reviewed training pipeline can consume
once a sufficiently large, disjoint, human-validated corpus exists.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = (
    "id",
    "bug_pattern",
    "code_snippet",
    "review_comment",
    "fix_summary",
    "provenance",
    "executable_evidence",
)
PROVENANCE_FIELDS = (
    "repository",
    "pull_request",
    "review_id",
    "source_commit",
    "fix_commit",
)
EVIDENCE_FIELDS = (
    "test_path",
    "test_command",
    "failure_before_fix",
    "passes_after_fix",
)


@dataclass(frozen=True)
class ReviewExample:
    id: str
    bug_pattern: str
    code_snippet: str
    review_comment: str
    fix_summary: str
    provenance: dict[str, str]
    executable_evidence: dict[str, Any]

    @classmethod
    def from_record(cls, record: dict[str, Any], line_number: int) -> ReviewExample:
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            raise ValueError(
                f"line {line_number}: missing required fields: {', '.join(missing)}"
            )
        text_fields = ("id", "bug_pattern", "code_snippet", "review_comment", "fix_summary")
        invalid_text = [
            field
            for field in text_fields
            if not isinstance(record.get(field), str) or not record[field].strip()
        ]
        if invalid_text:
            raise ValueError(
                f"line {line_number}: empty or non-string fields: {', '.join(invalid_text)}"
            )

        provenance = record["provenance"]
        if not isinstance(provenance, dict):
            raise ValueError(f"line {line_number}: provenance must be an object")
        missing_provenance = [
            field
            for field in PROVENANCE_FIELDS
            if not isinstance(provenance.get(field), str) or not provenance[field].strip()
        ]
        if missing_provenance:
            raise ValueError(
                f"line {line_number}: missing or empty provenance fields: {', '.join(missing_provenance)}"
            )

        evidence = record["executable_evidence"]
        if not isinstance(evidence, dict):
            raise ValueError(f"line {line_number}: executable_evidence must be an object")
        missing_evidence = [
            field
            for field in EVIDENCE_FIELDS
            if field not in evidence
            or (isinstance(evidence[field], str) and not evidence[field].strip())
        ]
        if missing_evidence:
            raise ValueError(
                f"line {line_number}: missing or empty executable evidence fields: {', '.join(missing_evidence)}"
            )
        for field in ("failure_before_fix", "passes_after_fix"):
            if evidence[field] is not True:
                raise ValueError(
                    f"line {line_number}: executable_evidence.{field} must be true"
                )

        return cls(
            id=record["id"].strip(),
            bug_pattern=record["bug_pattern"].strip(),
            code_snippet=record["code_snippet"].strip(),
            review_comment=record["review_comment"].strip(),
            fix_summary=record["fix_summary"].strip(),
            provenance={field: provenance[field].strip() for field in PROVENANCE_FIELDS},
            executable_evidence={field: evidence[field] for field in EVIDENCE_FIELDS},
        )

    def to_sft_record(self) -> dict[str, Any]:
        """Return a provider-neutral chat record with lineage metadata."""
        return {
            "id": self.id,
            "bug_pattern": self.bug_pattern,
            "provenance": self.provenance,
            "executable_evidence": self.executable_evidence,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a code reviewer. Report only concrete defects and "
                        "explain the executable behavior that demonstrates each defect."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Code under review:\n```\n{self.code_snippet}\n```",
                },
                {
                    "role": "assistant",
                    "content": (
                        f"Review comment:\n{self.review_comment}\n\n"
                        f"Fix summary:\n{self.fix_summary}"
                    ),
                },
            ],
        }


def load_review_examples(path: str | Path) -> list[ReviewExample]:
    """Load strictly validated training JSONL; reject malformed/duplicate records."""
    source = Path(path)
    examples: list[ReviewExample] = []
    seen_ids: set[str] = set()
    with source.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"line {line_number}: expected a JSON object")
            example = ReviewExample.from_record(record, line_number)
            if example.id in seen_ids:
                raise ValueError(f"line {line_number}: duplicate example id {example.id!r}")
            seen_ids.add(example.id)
            examples.append(example)
    if not examples:
        raise ValueError(f"{source}: no training examples found")
    return examples


def export_sft_jsonl(examples: Iterable[ReviewExample], output_path: str | Path) -> int:
    """Write validated examples as JSONL and return the number exported."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        for example in examples:
            handle.write(json.dumps(example.to_sft_record(), ensure_ascii=False) + "\n")
            count += 1
    if count == 0:
        raise ValueError("refusing to export an empty training dataset")
    return count


def example_as_record(example: ReviewExample) -> dict[str, Any]:
    """Expose a safe, testable representation for dataset audits."""
    return asdict(example)
