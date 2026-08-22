#!/usr/bin/env python3
"""Create a read-only calibration report from a Janus /metrics snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow execution as `python scripts/reviewer_calibration_report.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.calibration import report_from_prometheus_text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize Janus Reviewer telemetry without changing runtime settings."
    )
    parser.add_argument(
        "snapshot",
        type=Path,
        nargs="?",
        help="Prometheus text snapshot; reads stdin when omitted",
    )
    parser.add_argument(
        "--minimum-sample-size",
        type=int,
        default=30,
        help="sample size required before rates are considered sufficient (default: 30)",
    )
    parser.add_argument("--output", type=Path, help="optional JSON report destination")
    args = parser.parse_args()

    if args.minimum_sample_size < 1:
        parser.error("--minimum-sample-size must be at least 1")

    try:
        text = args.snapshot.read_text(encoding="utf-8") if args.snapshot else sys.stdin.read()
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    report = report_from_prometheus_text(
        text,
        minimum_sample_size=args.minimum_sample_size,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            args.output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
