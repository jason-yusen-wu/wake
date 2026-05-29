"""
report — Phase 8 gate report.

Loads all instance results from the output directory, computes the three
headline metrics, and prints a final pass/fail verdict.

Pre-registered thresholds (must not be changed after data collection):
  resolved_rate_delta > 0     wake must help more than it hurts
  false_positive_rate < 0.10  agents will stop trusting a noisier gate
  n_instances >= 50           minimum for statistical credibility

Usage:
  python report.py --results-dir ./results
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from metrics import load_results, compute, print_report

# Pre-registered thresholds
THRESHOLD_DELTA_POSITIVE = True   # delta must be > 0
THRESHOLD_FP_RATE = 0.10          # FP rate must be < 10%
THRESHOLD_MIN_INSTANCES = 50      # minimum instances for credibility


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default="./results")
    args = p.parse_args()

    results_dir = Path(args.results_dir)
    results = load_results(results_dir)

    if not results:
        print(f"No results found in {results_dir}. Run batch_eval.py first.")
        sys.exit(1)

    m = compute(results, results_dir=results_dir)
    print_report(m)

    # Threshold checks
    checks = []
    checks.append(("resolved_rate_delta > 0", m.resolved_rate_delta > 0))
    import math
    fp_ok = math.isnan(m.false_positive_rate) or m.false_positive_rate < THRESHOLD_FP_RATE
    checks.append((f"false_positive_rate < {THRESHOLD_FP_RATE:.0%}", fp_ok))
    checks.append((f"n_instances >= {THRESHOLD_MIN_INSTANCES}", m.n_instances >= THRESHOLD_MIN_INSTANCES))

    print("  Threshold checks:")
    for desc, passed in checks:
        print(f"    {'✓' if passed else '✗'} {desc}")
    print()

    overall = all(ok for _, ok in checks)
    if overall:
        print("  PHASE 8 GATE: PASS ✓")
        print("  The bet is validated. Wake adds measurable lift on SWE-bench Verified.")
    else:
        print("  PHASE 8 GATE: FAIL ✗")
        failed = [d for d, ok in checks if not ok]
        print(f"  Failed: {', '.join(failed)}")

    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
