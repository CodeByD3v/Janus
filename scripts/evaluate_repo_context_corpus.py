#!/usr/bin/env python3
"""Evaluate repository-context retrieval against an explicit local corpus manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow execution as `python scripts/evaluate_repo_context_corpus.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.corpus_eval import evaluate_corpus_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate repo-context retrieval against user-supplied local repositories."
    )
    parser.add_argument("manifest", type=Path, help="JSON corpus manifest")
    parser.add_argument("--output", type=Path, help="optional JSON report destination")
    args = parser.parse_args()

    try:
        report = evaluate_corpus_manifest(args.manifest)
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        print(rendered, end="")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
