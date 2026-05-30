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
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "clients" / "wake-py"))

from wake_harness import HarnessConfig, LoopResult, WakeHarness

CORPUS_DIR = Path(__file__).parent / "corpus"
SPECS_DIR = CORPUS_DIR / "specs"
RESULTS_DIR = Path(__file__).parent / "results"
MANIFEST_PATH = RESULTS_DIR / "manifest.json"

THRESHOLD_FIX_RATE = 0.70
THRESHOLD_DELTA = 0.0  # strictly positive

_PRINT_LOCK = threading.Lock()


def _safe_print(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


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

def _save_pair(pair: PairResult) -> None:
    """Persist one spec's result so a kill mid-run doesn't lose work."""
    payload = {
        "spec_id": pair.spec_id,
        "spec_name": pair.spec_name,
        "wake": {
            "success": pair.wake.success,
            "iterations": pair.wake.iterations,
            "latency_ms": pair.wake.latency_ms,
        },
        "ablation": {
            "success": pair.ablation.success,
            "iterations": pair.ablation.iterations,
            "latency_ms": pair.ablation.latency_ms,
        },
    }
    _atomic_write(RESULTS_DIR / f"{pair.spec_id}.json", json.dumps(payload, indent=2))


def _run_one_spec(
    spec_path: Path,
    daemon_path: str,
    model: str,
    budget: int,
) -> PairResult:
    spec = load_spec(spec_path)
    spec_id = spec["id"]
    spec_name = spec["name"]
    task = spec["task"]
    primary_path = resolve_file(spec["primary_file"])
    primary_uri = build_uri(primary_path)
    files = build_file_map(spec, use_fixed=False)

    _safe_print(f"[{spec_id}] {spec_name}  task={task[:60]}...")

    cfg_wake = HarnessConfig(daemon_path=daemon_path, model=model, budget=budget, ablation=False)
    result_wake = WakeHarness(cfg_wake).run(files, primary_uri, task)
    cfg_abl  = HarnessConfig(daemon_path=daemon_path, model=model, budget=budget, ablation=True)
    result_abl = WakeHarness(cfg_abl).run(files, primary_uri, task)

    pair = PairResult(spec_id=spec_id, spec_name=spec_name, wake=result_wake, ablation=result_abl)
    _save_pair(pair)
    ws = "PASS" if result_wake.success else f"FAIL@{result_wake.iterations}"
    as_ = "PASS" if result_abl.success else f"FAIL@{result_abl.iterations}"
    _safe_print(f"[{spec_id}] wake={ws}  ablation={as_}")
    return pair


def run(
    daemon_path: str = "wake-daemon",
    model: str = "claude-sonnet-4-6",
    budget: int = 5,
    spec_filter: list[str] | None = None,
    workers: int = 4,
    resume: bool = False,
) -> list[PairResult]:
    specs = sorted(SPECS_DIR.glob("*.json"))
    if not specs:
        print("No spec files found in", SPECS_DIR)
        sys.exit(1)

    if spec_filter:
        specs = [s for s in specs if any(f in s.stem for f in spec_filter)]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if resume:
        specs = [s for s in specs if not (RESULTS_DIR / f"{load_spec(s)['id']}.json").exists()]
        if not specs:
            print("Nothing to do (all specs already have results; remove --resume to rerun).")
            return _load_existing_results()

    # Persistent manifest so a kill mid-run leaves a usable record.
    manifest = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "workers": workers,
        "scope": [load_spec(s)["id"] for s in specs],
        "instances": {},
        "finished_at": None,
    }
    _atomic_write(MANIFEST_PATH, json.dumps(manifest, indent=2))

    def _record(spec_id: str, entry: dict) -> None:
        manifest["instances"][spec_id] = {
            **entry, "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _atomic_write(MANIFEST_PATH, json.dumps(manifest, indent=2))

    results: list[PairResult] = []
    t0 = time.perf_counter()
    if workers <= 1:
        for spec_path in specs:
            try:
                pair = _run_one_spec(spec_path, daemon_path, model, budget)
                results.append(pair)
                _record(pair.spec_id, {
                    "status": "ok",
                    "wake_success": pair.wake.success,
                    "ablation_success": pair.ablation.success,
                })
            except Exception as exc:
                spec_id = load_spec(spec_path)["id"]
                _safe_print(f"[{spec_id}] EXC: {exc}")
                _record(spec_id, {"status": "error", "error": str(exc)[:200]})
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_run_one_spec, sp, daemon_path, model, budget): sp
                for sp in specs
            }
            for fut in as_completed(futures):
                sp = futures[fut]
                try:
                    pair = fut.result()
                    results.append(pair)
                    _record(pair.spec_id, {
                        "status": "ok",
                        "wake_success": pair.wake.success,
                        "ablation_success": pair.ablation.success,
                    })
                except Exception as exc:
                    spec_id = load_spec(sp)["id"]
                    _safe_print(f"[{spec_id}] EXC: {exc}")
                    _record(spec_id, {"status": "error", "error": str(exc)[:200]})

    manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    manifest["wall_time_s"] = round(time.perf_counter() - t0, 1)
    _atomic_write(MANIFEST_PATH, json.dumps(manifest, indent=2))

    # If resume, fold previously-saved results into the returned list.
    results.extend(_load_existing_results(exclude_ids={r.spec_id for r in results}))
    return results


def _load_existing_results(exclude_ids: set[str] | None = None) -> list[PairResult]:
    """Rebuild PairResult objects from disk for the summary."""
    out: list[PairResult] = []
    exclude_ids = exclude_ids or set()
    for p in sorted(RESULTS_DIR.glob("*.json")):
        if p.name == "manifest.json":
            continue
        d = json.loads(p.read_text())
        if d.get("spec_id") in exclude_ids:
            continue
        out.append(PairResult(
            spec_id=d["spec_id"],
            spec_name=d["spec_name"],
            wake=LoopResult(
                success=d["wake"]["success"],
                iterations=d["wake"]["iterations"],
                final_text="",
                regressions_caught=[],
                latency_ms=d["wake"].get("latency_ms", []),
            ),
            ablation=LoopResult(
                success=d["ablation"]["success"],
                iterations=d["ablation"]["iterations"],
                final_text="",
                regressions_caught=[],
                latency_ms=d["ablation"].get("latency_ms", []),
            ),
        ))
    return out


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
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel specs (each spawns its own daemon)")
    p.add_argument("--resume", action="store_true",
                   help="skip specs that already have a result file")
    args = p.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print(f"Running agent loop evaluation  (workers={args.workers})...")
    results = run(
        daemon_path=args.daemon,
        model=args.model,
        budget=args.budget,
        spec_filter=args.cases,
        workers=args.workers,
        resume=args.resume,
    )
    passed = print_summary(results)
    sys.exit(0 if passed else 1)
