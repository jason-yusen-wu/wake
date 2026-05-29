"""
probe/oracle/eval.py — compute the Rung 2 oracle lift ceiling.

Loads results from harness.py (oracle vs ablation runs) and reports:

  1. Oracle fix rate  — fraction of cases where the oracle arm produced a patch
  2. Ablation fix rate — fraction without any analysis feedback
  3. Ceiling delta   — oracle - ablation

  The ceiling delta is the maximum lift the real engine can achieve.
  If it's near zero: no analysis-based feedback will move the needle —
  reconsider the premise.
  If it's positive: the project is "close this gap" — tractable engineering.

  Stratified breakdown by property (nullability vs change_consistency) lets
  you see whether the lift is concentrated in the properties wake implements.

Usage:
  python eval.py
  python eval.py --results-dir results
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
FEEDBACK_DIR = Path(__file__).parent / "feedback"


@dataclass
class OracleMetrics:
    n_instances: int
    oracle_success: int
    ablation_success: int
    oracle_rate: float
    ablation_rate: float
    ceiling_delta: float
    # Stratified
    by_property: dict[str, tuple[int, int, int]]  # property → (n, oracle_ok, abl_ok)
    # Confidence breakdown
    high_confidence_delta: float
    n_errors: int


def compute(results_dir: Path) -> OracleMetrics:
    result_files = sorted(results_dir.glob("*.json"))
    if not result_files:
        return OracleMetrics(
            n_instances=0, oracle_success=0, ablation_success=0,
            oracle_rate=0.0, ablation_rate=0.0, ceiling_delta=0.0,
            by_property={}, high_confidence_delta=float("nan"), n_errors=0,
        )

    # Load feedback metadata for stratification.
    feedback_meta: dict[str, dict] = {}
    for ff in FEEDBACK_DIR.glob("*.json"):
        fb = json.loads(ff.read_text())
        feedback_meta[fb["instance_id"]] = fb

    records = []
    for rp in result_files:
        data = json.loads(rp.read_text())
        records.append(data)

    n = len(records)
    oracle_ok   = sum(1 for r in records if r.get("oracle", {}).get("success"))
    ablation_ok = sum(1 for r in records if r.get("ablation", {}).get("success"))
    n_errors = sum(
        1 for r in records
        if r.get("oracle", {}).get("error") or r.get("ablation", {}).get("error")
    )

    # Stratify by property.
    by_prop: dict[str, list] = {}
    for r in records:
        iid = r["instance_id"]
        prop = feedback_meta.get(iid, {}).get("which_property", "unknown")
        by_prop.setdefault(prop, []).append(r)

    by_property: dict[str, tuple[int, int, int]] = {}
    for prop, items in by_prop.items():
        ok_o = sum(1 for r in items if r.get("oracle", {}).get("success"))
        ok_a = sum(1 for r in items if r.get("ablation", {}).get("success"))
        by_property[prop] = (len(items), ok_o, ok_a)

    # High-confidence subset.
    high = [
        r for r in records
        if feedback_meta.get(r["instance_id"], {}).get("confidence") == "high"
    ]
    h_delta = float("nan")
    if high:
        h_o = sum(1 for r in high if r.get("oracle", {}).get("success"))
        h_a = sum(1 for r in high if r.get("ablation", {}).get("success"))
        h_delta = (h_o - h_a) / len(high)

    return OracleMetrics(
        n_instances=n,
        oracle_success=oracle_ok,
        ablation_success=ablation_ok,
        oracle_rate=oracle_ok / n if n else 0.0,
        ablation_rate=ablation_ok / n if n else 0.0,
        ceiling_delta=(oracle_ok - ablation_ok) / n if n else 0.0,
        by_property=by_property,
        high_confidence_delta=h_delta,
        n_errors=n_errors,
    )


def _pct(v: float) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:+.1%}" if v != 0 else "0.0%"


def print_report(m: OracleMetrics) -> None:
    W = 65
    print()
    print("=" * W)
    print("RUNG 2 — ORACLE LIFT CEILING")
    print("=" * W)
    print(f"  Instances:      {m.n_instances}   Errors: {m.n_errors}")
    print()
    print(f"  Oracle fix rate:    {m.oracle_success}/{m.n_instances} ({m.oracle_rate:.1%})")
    print(f"  Ablation fix rate:  {m.ablation_success}/{m.n_instances} ({m.ablation_rate:.1%})")
    print(f"  Ceiling delta:      {_pct(m.ceiling_delta)}   ← max achievable lift")
    if not math.isnan(m.high_confidence_delta):
        print(f"  High-confidence:    {_pct(m.high_confidence_delta)}")
    print()
    if m.by_property:
        print("  STRATIFIED BY PROPERTY")
        print(f"  {'Property':<25} {'N':>4}  {'Oracle':>7}  {'Ablation':>8}  {'Delta':>7}")
        print("  " + "─" * 57)
        for prop, (n, ok_o, ok_a) in sorted(m.by_property.items(), key=lambda x: -x[1][0]):
            o_r = ok_o / n if n else 0.0
            a_r = ok_a / n if n else 0.0
            d = o_r - a_r
            print(f"  {prop:<25} {n:>4}  {ok_o:>3}/{n} ({o_r:.0%})  "
                  f"{ok_a:>3}/{n} ({a_r:.0%})  {_pct(d):>7}")
        print()

    print("  INTERPRETATION")
    print("  " + "─" * 40)
    if m.n_instances == 0:
        print("  No results yet.  Run harness.py first.")
    elif m.ceiling_delta <= 0:
        print("  ✗ Zero or negative ceiling delta.")
        print("    Perfect feedback does not help the model fix these bugs.")
        print("    The premise is challenged: reconsider before investing in the engine.")
    elif m.ceiling_delta < 0.10:
        print(f"  ~ Small ceiling delta ({_pct(m.ceiling_delta)}).")
        print("    Analysis feedback provides marginal lift.  Proceed cautiously.")
    else:
        print(f"  ✓ Positive ceiling delta ({_pct(m.ceiling_delta)}).")
        print("    Perfect feedback lifts the resolved rate.")
        print("    The project is now: 'how close can automated analysis get to this ceiling?'")
        print(f"    Phase 8 baseline to beat:  oracle_rate = {m.oracle_rate:.1%}")
    print("=" * W)


def main() -> None:
    p = argparse.ArgumentParser(description="Compute Rung 2 oracle lift ceiling.")
    p.add_argument("--results-dir", default=str(RESULTS_DIR))
    args = p.parse_args()

    m = compute(Path(args.results_dir))
    print_report(m)


if __name__ == "__main__":
    main()
