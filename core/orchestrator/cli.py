"""CLI utilities for printing debate results."""

from __future__ import annotations

from core.orchestrator.models import DebateResult


def print_debate_summary(result: DebateResult) -> None:
    """Print a human-readable summary of a debate result (for CLI use)."""
    print(f"Sandbox: {result.sandbox_path}")
    print(f"Verdict: {result.reviewer_verdict}")
    if result.needs_human_review:
        print("⚠ Flagged for human review (INCONCLUSIVE)")
    for r in result.rounds:
        if r.round_num == 0:
            print("\n--- Initial Review (Round 0) ---")
            print(f"Verdict: {r.reviewer_verdict}")
        else:
            print(f"\n--- Round {r.round_num} ---")
            print(f"Verdict: {r.reviewer_verdict}")
        print("Reviewer:", r.reviewer_text[:400])
        if r.gate_result and r.gate_result.get("passed") is not None:
            print("Gate at this round:", "PASS" if r.gate_result["passed"] else "FAIL")
        if r.code_extraction_failed:
            print("  ⚠ Code extraction failed this round")
        if r.reviewer_skipped_counterexample:
            print("  ⚠ Reviewer gave critique without a counterexample test")
        if r.stop_reason:
            print("Stop reason:", r.stop_reason)
    if result.final_gate:
        print("\n=== FINAL GATE ===")
        for c in result.final_gate["checks"]:
            print(f"  [{'OK' if c['passed'] else 'FAIL'}] {c['check']}")
        print("MERGED" if result.merged else "REJECTED — did not pass the gate")
    if result.cost:
        print(f"\nCost: {result.cost}")
