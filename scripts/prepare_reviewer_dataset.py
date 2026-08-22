#!/usr/bin/env python3
"""Prepare a provider-neutral Reviewer SFT JSONL file from real-catch data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow execution as `python scripts/prepare_reviewer_dataset.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from training.dataset import export_sft_jsonl, load_review_examples


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate real-catch JSONL and export provider-neutral Reviewer SFT JSONL."
    )
    parser.add_argument("input", type=Path, help="source real-catch JSONL")
    parser.add_argument("output", type=Path, help="destination SFT JSONL")
    args = parser.parse_args()

    try:
        examples = load_review_examples(args.input)
        count = export_sft_jsonl(examples, args.output)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"validated and exported {count} examples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
