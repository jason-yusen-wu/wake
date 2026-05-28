"""
report — Phase 7 gate report. Runs both Part A and Part B and prints a
combined pass/fail verdict.

Usage:
  python report.py --daemon ./target/debug/wake-daemon [--model claude-sonnet-4-6] [--budget 5] [--skip-loop]

  --skip-loop   Run only Part A (no API calls). Useful for CI where no API key is available.
"""
from __future__ import annotations

import argparse
import os
import sys

import corpus_eval
import loop_eval

PHASE7_THRESHOLDS = {
    "catch_rate":    ("≥ 90%",  corpus_eval.THRESHOLD_CATCH_RATE),
    "fp_rate":       ("= 0%",   corpus_eval.THRESHOLD_FP_RATE),
    "witness_concrete": (f"≥ {corpus_eval.THRESHOLD_WITNESS_CONCRETE:.0%}", corpus_eval.THRESHOLD_WITNESS_CONCRETE),
    "latency_ms":    (f"< {corpus_eval.THRESHOLD_LATENCY_MS:.0f}ms", corpus_eval.THRESHOLD_LATENCY_MS),
    "fix_rate_wake": (f"≥ {loop_eval.THRESHOLD_FIX_RATE:.0%}", loop_eval.THRESHOLD_FIX_RATE),
    "delta":         ("> 0%",   loop_eval.THRESHOLD_DELTA),
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--daemon", default="wake-daemon")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--budget", type=int, default=5)
    p.add_argument("--skip-loop", action="store_true", help="Skip Part B (no API calls)")
    args = p.parse_args()

    print("=" * 65)
    print("PHASE 7 VALIDATION REPORT")
    print("=" * 65)
    print()
    print("Pre-registered thresholds:")
    for name, (desc, _) in PHASE7_THRESHOLDS.items():
        print(f"  {name:<22}: {desc}")
    print()

    # Part A — static corpus evaluation
    print("─" * 65)
    print("PART A: Static analysis corpus (no API)")
    print("─" * 65)
    corpus_results = corpus_eval.run(args.daemon)
    part_a_pass = corpus_eval.print_summary(corpus_results)

    # Part B — agent loop evaluation
    part_b_pass = True
    if not args.skip_loop:
        if "ANTHROPIC_API_KEY" not in os.environ:
            print("Warning: ANTHROPIC_API_KEY not set — skipping Part B")
            part_b_pass = False
        else:
            print("─" * 65)
            print("PART B: Agent loop + ablation (API calls)")
            print("─" * 65)
            loop_results = loop_eval.run(
                daemon_path=args.daemon,
                model=args.model,
                budget=args.budget,
            )
            part_b_pass = loop_eval.print_summary(loop_results)
    else:
        print("(Part B skipped — run without --skip-loop to include agent loop)")

    # Overall verdict
    overall = part_a_pass and part_b_pass
    print("=" * 65)
    print(f"PHASE 7 GATE: {'PASS ✓ — ready for Phase 8 (SWE-bench eval)' if overall else 'FAIL ✗ — do not proceed to Phase 8'}")
    print("=" * 65)
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
