"""
metrics — compute the three Phase 8 headline metrics from batch_eval results.

Metric definitions (design doc §8):

1. Resolved-rate delta
   wake_resolved_rate - ablation_resolved_rate
   Positive = wake helps. This is the headline lift number.

2. Regression-catch rate  (operationalizes the 80/20 claim)
   Numerator:   instances where ablation FAILED a held-out test AND wake
                fired during the run (wake caught something the tests missed)
   Denominator: instances where ablation failed any held-out test
   = "of the bugs the ablation agent introduced that failed held-out tests,
      what fraction did wake flag during its run?"

3. False-positive rate
   Numerator:   instances where wake fired AND the patch was correct
                (all tests pass in the wake arm)
   Denominator: instances where the wake arm produced a correct patch
   = "of correctly-resolved instances, what fraction triggered the gate?"
   Goal: as low as possible. High FP rate means agents will distrust wake.

Secondary metrics:
  - avg cold-start latency per instance
  - avg warm-query latency per wake firing
  - fraction of instances where wake returned Unknown (coverage honesty)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from task_runner import InstanceResult, ArmResult, _load_result


@dataclass
class EvalMetrics:
    n_instances: int
    # Primary
    wake_resolved: int
    ablation_resolved: int
    resolved_rate_wake: float
    resolved_rate_ablation: float
    resolved_rate_delta: float
    # Metric 2
    ablation_failed_held_out: int       # denominator
    wake_caught_held_out_fail: int      # numerator
    regression_catch_rate: float
    # Metric 3
    wake_correct_patches: int           # denominator
    wake_fired_on_correct: int          # numerator
    false_positive_rate: float
    # Secondary
    avg_cold_start_ms: float
    avg_warm_query_ms: float
    # Errors
    n_errors: int


def compute(results: list[InstanceResult], results_dir: Path | None = None) -> EvalMetrics:
    n = len(results)
    wake_res = sum(1 for r in results if r.wake.resolved)
    abl_res = sum(1 for r in results if r.ablation.resolved)

    # Metric 2: regression-catch rate
    # ablation failed a held-out test = NOT resolved (any FAIL_TO_PASS test failed)
    abl_failed_held_out = sum(
        1 for r in results
        if not r.ablation.resolved and any(
            not v for v in r.ablation.fail_to_pass_results.values()
        )
    )
    wake_caught = sum(
        1 for r in results
        if not r.ablation.resolved
        and any(not v for v in r.ablation.fail_to_pass_results.values())
        and r.wake.wake_fired
    )
    catch_rate = wake_caught / abl_failed_held_out if abl_failed_held_out else float("nan")

    # Metric 3: false-positive rate
    # Wake correct = wake arm resolved the instance
    wake_correct = sum(1 for r in results if r.wake.resolved)
    wake_fp = sum(1 for r in results if r.wake.resolved and r.wake.wake_fired)
    fp_rate = wake_fp / wake_correct if wake_correct else float("nan")

    # Secondary: latency from per-instance wake logs
    cold_starts: list[float] = []
    warm_queries: list[float] = []
    if results_dir:
        for r in results:
            log_path = results_dir / r.instance_id / "wake" / f"{r.instance_id}_wake.json"
            if log_path.exists():
                log = json.loads(log_path.read_text())
                if cs := log.get("cold_start_ms"):
                    cold_starts.append(cs)
                for f in log.get("findings", []):
                    warm_queries.append(f.get("latency_ms", 0))

    avg_cold = sum(cold_starts) / len(cold_starts) if cold_starts else 0.0
    avg_warm = sum(warm_queries) / len(warm_queries) if warm_queries else 0.0

    n_errors = sum(1 for r in results if r.wake.error or r.ablation.error)

    return EvalMetrics(
        n_instances=n,
        wake_resolved=wake_res,
        ablation_resolved=abl_res,
        resolved_rate_wake=wake_res / n if n else 0.0,
        resolved_rate_ablation=abl_res / n if n else 0.0,
        resolved_rate_delta=(wake_res - abl_res) / n if n else 0.0,
        ablation_failed_held_out=abl_failed_held_out,
        wake_caught_held_out_fail=wake_caught,
        regression_catch_rate=catch_rate,
        wake_correct_patches=wake_correct,
        wake_fired_on_correct=wake_fp,
        false_positive_rate=fp_rate,
        avg_cold_start_ms=avg_cold,
        avg_warm_query_ms=avg_warm,
        n_errors=n_errors,
    )


def load_results(results_dir: Path) -> list[InstanceResult]:
    results = []
    for result_path in sorted(results_dir.glob("*/result.json")):
        results.append(_load_result(result_path))
    return results


def _pct(v: float) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:.1%}"


def print_report(m: EvalMetrics) -> None:
    print()
    print("=" * 65)
    print("PHASE 8 — EVALUATION METRICS")
    print("=" * 65)
    print(f"  Instances evaluated:    {m.n_instances}")
    print(f"  Errors:                 {m.n_errors}")
    print()
    print("  PRIMARY METRICS")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  1. Resolved-rate delta")
    print(f"       Wake arm:           {m.wake_resolved}/{m.n_instances} ({_pct(m.resolved_rate_wake)})")
    print(f"       Ablation arm:       {m.ablation_resolved}/{m.n_instances} ({_pct(m.resolved_rate_ablation)})")
    print(f"       Delta:              {m.resolved_rate_delta:+.1%}  {'← headline lift' if m.resolved_rate_delta > 0 else '← no lift'}")
    print()
    print(f"  2. Regression-catch rate  (held-out breaks caught)")
    print(f"       Ablation failed held-out tests: {m.ablation_failed_held_out} instances")
    print(f"       Wake fired on those:            {m.wake_caught_held_out_fail}")
    print(f"       Catch rate:                     {_pct(m.regression_catch_rate)}")
    print()
    print(f"  3. False-positive rate  (trust metric)")
    print(f"       Correctly-resolved by wake:     {m.wake_correct_patches}")
    print(f"       Wake fired on those:            {m.wake_fired_on_correct}")
    print(f"       FP rate:                        {_pct(m.false_positive_rate)}")
    print()
    print("  SECONDARY METRICS")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Avg cold-start latency:   {m.avg_cold_start_ms:.0f}ms")
    print(f"  Avg warm-query latency:   {m.avg_warm_query_ms:.0f}ms")
    print()

    # Gate verdict
    gate = (
        m.resolved_rate_delta > 0
        and (math.isnan(m.false_positive_rate) or m.false_positive_rate < 0.10)
        and m.n_errors < m.n_instances * 0.2  # fewer than 20% errored
    )
    print(f"  PHASE 8 GATE: {'PASS ✓' if gate else 'FAIL ✗'}")
    print("=" * 65)


if __name__ == "__main__":
    import argparse, sys
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default="./results")
    args = p.parse_args()

    results_dir = Path(args.results_dir)
    results = load_results(results_dir)
    if not results:
        print(f"No results found in {results_dir}")
        sys.exit(1)

    m = compute(results, results_dir=results_dir)
    print_report(m)
