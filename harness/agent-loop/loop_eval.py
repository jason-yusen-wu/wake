"""
loop_eval — Part B of Phase 7 validation. Requires ANTHROPIC_API_KEY.

For each spec, runs the CEGIS loop twice:
  1. Wake-guided:   model receives full wake feedback (regressions + witnesses + def-use).
  2. Ablation:      model receives only "try again" — no wake analysis in the message.

Both arms use the same budget. Metrics:
  fix_rate_wake:      fraction of cases fixed within budget (with feedback)
  fix_rate_ablation:  fraction of cases fixed within budget (without feedback)
  delta:              fix_rate_wake - fix_rate_ablation  (must be > 0)
  avg_iters_wake:     average iterations to fix (wake-guided)
  avg_iters_ablation: average iterations to fix (ablation)

Pre-registered thresholds (Phase 7 gate):
  fix_rate_wake      >= 70%
  fix_rate_ablation  any value (baseline)
  delta              > 0   (wake MUST add measurable value)
"""
from __future__ import annotations

import json
import sys
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "clients" / "wake-py"))

from wake_harness import HarnessConfig, LoopResult, WakeHarness

CORPUS_DIR = Path(__file__).parent / "corpus"
SPECS_DIR = CORPUS_DIR / "specs"

THRESHOLD_FIX_RATE = 0.70
THRESHOLD_DELTA = 0.0  # strictly positive


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class PairResult:
    spec_id: str
    spec_name: str
    wake: LoopResult
    ablation: LoopResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_spec(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def resolve_file(spec_path_str: str) -> Path:
    return CORPUS_DIR / spec_path_str


def build_uri(file_path: Path) -> str:
    return "file://" + str(file_path.resolve())


def build_file_map(spec: dict, use_fixed: bool = False) -> dict[str, str]:
    """Build {uri: text} for all files in the spec."""
    file_list = spec["fixed_files"] if use_fixed else spec["files"]
    result = {}
    for fspec in file_list:
        p = resolve_file(fspec["path"])
        result[build_uri(p)] = p.read_text()
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    daemon_path: str = "wake-daemon",
    model: str = "claude-sonnet-4-6",
    budget: int = 5,
    spec_filter: list[str] | None = None,
) -> list[PairResult]:
    specs = sorted(SPECS_DIR.glob("*.json"))
    if not specs:
        print("No spec files found in", SPECS_DIR)
        sys.exit(1)

    if spec_filter:
        specs = [s for s in specs if any(f in s.stem for f in spec_filter)]

    results: list[PairResult] = []

    for spec_path in specs:
        spec = load_spec(spec_path)
        spec_id = spec["id"]
        spec_name = spec["name"]
        task = spec["task"]

        primary_path = resolve_file(spec["primary_file"])
        primary_uri = build_uri(primary_path)
        files = build_file_map(spec, use_fixed=False)

        print(f"\n[{spec_id}] {spec_name}")
        print(f"  Task: {task[:80]}...")

        # --- Wake-guided run ---
        print("  Wake-guided ... ", end="", flush=True)
        cfg_wake = HarnessConfig(
            daemon_path=daemon_path,
            model=model,
            budget=budget,
            ablation=False,
        )
        harness_wake = WakeHarness(cfg_wake)
        result_wake = harness_wake.run(files, primary_uri, task)
        status = "SUCCESS" if result_wake.success else f"FAIL ({result_wake.iterations} iters)"
        print(status, f"  latencies: {[f'{l:.0f}ms' for l in result_wake.latency_ms]}")

        # --- Ablation run (restore original files first) ---
        print("  Ablation    ... ", end="", flush=True)
        cfg_abl = HarnessConfig(
            daemon_path=daemon_path,
            model=model,
            budget=budget,
            ablation=True,
        )
        harness_abl = WakeHarness(cfg_abl)
        result_abl = harness_abl.run(files, primary_uri, task)
        status = "SUCCESS" if result_abl.success else f"FAIL ({result_abl.iterations} iters)"
        print(status)

        results.append(PairResult(
            spec_id=spec_id,
            spec_name=spec_name,
            wake=result_wake,
            ablation=result_abl,
        ))

    return results


def print_summary(results: list[PairResult]) -> bool:
    total = len(results)
    wake_fixed = sum(1 for r in results if r.wake.success)
    abl_fixed = sum(1 for r in results if r.ablation.success)

    fix_rate_wake = wake_fixed / total if total else 0.0
    fix_rate_abl = abl_fixed / total if total else 0.0
    delta = fix_rate_wake - fix_rate_abl

    wake_iters = [r.wake.iterations for r in results if r.wake.success]
    abl_iters = [r.ablation.iterations for r in results if r.ablation.success]
    avg_wake = sum(wake_iters) / len(wake_iters) if wake_iters else float("nan")
    avg_abl = sum(abl_iters) / len(abl_iters) if abl_iters else float("nan")

    all_latencies = [l for r in results for l in r.wake.latency_ms]
    avg_latency = sum(all_latencies) / len(all_latencies) if all_latencies else 0.0

    print()
    print("=" * 60)
    print("LOOP EVAL — AGENT + WAKE (Part B)")
    print("=" * 60)
    print(f"  Fix rate (wake):      {wake_fixed}/{total} ({fix_rate_wake:.0%})  [threshold: ≥{THRESHOLD_FIX_RATE:.0%}]")
    print(f"  Fix rate (ablation):  {abl_fixed}/{total} ({fix_rate_abl:.0%})")
    print(f"  Delta (wake - abl):   {delta:+.0%}  [threshold: >0%]")
    print(f"  Avg iters (wake):     {avg_wake:.1f}")
    print(f"  Avg iters (ablation): {avg_abl:.1f}")
    print(f"  Avg wake latency:     {avg_latency:.0f}ms")
    print()

    print("  Per-case results:")
    print(f"  {'ID':<4} {'Name':<25} {'Wake':<8} {'Ablation':<8} {'Iters(W)'}")
    print(f"  {'-'*4} {'-'*25} {'-'*8} {'-'*8} {'-'*8}")
    for r in results:
        ws = "PASS" if r.wake.success else "FAIL"
        as_ = "PASS" if r.ablation.success else "FAIL"
        print(f"  {r.spec_id:<4} {r.spec_name:<25} {ws:<8} {as_:<8} {r.wake.iterations}")
    print()

    gate_pass = fix_rate_wake >= THRESHOLD_FIX_RATE and delta > THRESHOLD_DELTA
    print(f"  PART B GATE: {'PASS ✓' if gate_pass else 'FAIL ✗'}")
    return gate_pass


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--daemon", default="wake-daemon", help="Path to wake-daemon binary")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--budget", type=int, default=5)
    p.add_argument("--cases", nargs="*", help="Subset of case IDs to run (e.g. 01 03 09)")
    args = p.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print("Running agent loop evaluation (API calls will be made)...")
    results = run(
        daemon_path=args.daemon,
        model=args.model,
        budget=args.budget,
        spec_filter=args.cases,
    )
    passed = print_summary(results)
    sys.exit(0 if passed else 1)
