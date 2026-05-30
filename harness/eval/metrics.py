"""
metrics — compute Phase 8 headline metrics from a Path-A batch_eval run.

Reads:
  - swebench eval reports at:
        logs/run_evaluation/<run_id>/<model_name>/<instance_id>/report.json
    where <model_name> is the arm name (RunBatch's traj_dir.name).
  - WakeHook per-task logs at:
        <results_dir>/wake/<instance_id>/<instance_id>_wake.json
  - Run manifest at:
        reports/phase8_manifest.json   (for the swebench run_ids)
  - Rung 1 labeled audit at:
        probe/audit/corpus/labeled_failures.jsonl   (for stratification)

Three headline metrics (design doc §8):

  1. Resolved-rate delta = (wake_resolved - ablation_resolved) / n_paired

  2. Regression-catch rate
       denominator = ablation arm broke a PASS_TO_PASS test
       numerator   = wake arm fired on those instances
     (Lower-bound estimate: in the wake arm, the agent saw feedback and may
      have prevented the regression rather than firing on it.)

  3. False-positive rate
       denominator = instances where wake arm produced a correct patch
       numerator   = wake fired on those instances

Stratification: analyzable (Rung-1 labeled yes/partial) vs non-analyzable.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

AUDIT_DATASET = (
    Path(__file__).parent.parent.parent
    / "probe" / "audit" / "corpus" / "labeled_failures.jsonl"
)
SWEBENCH_LOG_ROOT = Path("logs") / "run_evaluation"   # relative to CWD by default


# ---------------------------------------------------------------------------
# Per-arm result extraction
# ---------------------------------------------------------------------------

@dataclass
class ArmResult:
    arm: str                       # "wake" | "ablation"
    instance_id: str
    resolved: bool
    fail_to_pass_pass: int = 0
    fail_to_pass_fail: int = 0
    pass_to_pass_pass: int = 0
    pass_to_pass_fail: int = 0
    wake_fired: bool = False       # only meaningful for arm="wake"
    error: str = ""

    @property
    def broke_pass_to_pass(self) -> bool:
        return self.pass_to_pass_fail > 0


@dataclass
class InstancePair:
    instance_id: str
    wake: ArmResult | None
    ablation: ArmResult | None


def _read_swebench_report(path: Path, arm: str, instance_id: str) -> ArmResult | None:
    """
    Per-instance swebench report.  Structure:
      {
        "<instance_id>": {
          "patch_is_None": bool,
          "patch_successfully_applied": bool,
          "resolved": bool,
          "tests_status": {
            "FAIL_TO_PASS": {"success": [...], "failure": [...]},
            "PASS_TO_PASS": {"success": [...], "failure": [...]}
          }
        }
      }
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return ArmResult(arm=arm, instance_id=instance_id, resolved=False,
                         error="report.json was not valid JSON")
    # The report is keyed by instance_id at the top level.
    inst = data.get(instance_id) or next(iter(data.values()), {})
    tests = inst.get("tests_status", {})
    ftp = tests.get("FAIL_TO_PASS", {})
    ptp = tests.get("PASS_TO_PASS", {})
    return ArmResult(
        arm=arm,
        instance_id=instance_id,
        resolved=bool(inst.get("resolved", False)),
        fail_to_pass_pass=len(ftp.get("success", [])),
        fail_to_pass_fail=len(ftp.get("failure", [])),
        pass_to_pass_pass=len(ptp.get("success", [])),
        pass_to_pass_fail=len(ptp.get("failure", [])),
    )


def _wake_fired(results_dir: Path, instance_id: str) -> bool:
    """Read WakeHook's per-task log to see whether wake produced any findings."""
    log = results_dir / "wake" / instance_id / f"{instance_id}_wake.json"
    if not log.exists():
        return False
    try:
        data = json.loads(log.read_text())
    except json.JSONDecodeError:
        return False
    return len(data.get("findings", [])) > 0


def _find_run_id_dir(arm: str, manifest_path: Path) -> Path | None:
    """
    The manifest records each arm's swebench run_id.  Map that to the
    swebench log dir.  Falls back to globbing for the latest dir with the
    "phase8_<arm>_" prefix.
    """
    if manifest_path.exists():
        man = json.loads(manifest_path.read_text())
        run_id = man.get("arms", {}).get(arm, {}).get("run_id")
        if run_id:
            candidate = SWEBENCH_LOG_ROOT / run_id
            if candidate.exists():
                return candidate

    # Fallback: latest matching dir
    candidates = sorted(
        SWEBENCH_LOG_ROOT.glob(f"phase8_{arm}_*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    return candidates[-1] if candidates else None


def _collect_arm(arm: str, results_dir: Path, manifest_path: Path) -> dict[str, ArmResult]:
    """Walk swebench reports for one arm and build {instance_id: ArmResult}."""
    out: dict[str, ArmResult] = {}
    run_dir = _find_run_id_dir(arm, manifest_path)
    if run_dir is None:
        return out
    # Layout: <run_dir>/<model_name>/<instance_id>/report.json
    # model_name is the predictions traj_dir.name (= arm name for us).
    for model_dir in run_dir.iterdir():
        if not model_dir.is_dir():
            continue
        for inst_dir in model_dir.iterdir():
            if not inst_dir.is_dir():
                continue
            instance_id = inst_dir.name
            report = _read_swebench_report(
                inst_dir / "report.json", arm, instance_id,
            )
            if report is None:
                continue
            if arm == "wake":
                report.wake_fired = _wake_fired(results_dir, instance_id)
            out[instance_id] = report
    return out


def collect_pairs(results_dir: Path, manifest_path: Path) -> list[InstancePair]:
    wake = _collect_arm("wake", results_dir, manifest_path)
    ablation = _collect_arm("ablation", results_dir, manifest_path)
    ids = sorted(set(wake) | set(ablation))
    return [
        InstancePair(instance_id=i, wake=wake.get(i), ablation=ablation.get(i))
        for i in ids
    ]


# ---------------------------------------------------------------------------
# Stratification
# ---------------------------------------------------------------------------

def _load_audit_metadata(path: Path = AUDIT_DATASET) -> dict[str, dict]:
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[r["instance_id"]] = r
    return out


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class StratumMetrics:
    name: str
    n: int
    n_paired: int                                # instances where both arms have results
    wake_resolved: int
    ablation_resolved: int
    resolved_rate_wake: float
    resolved_rate_ablation: float
    resolved_rate_delta: float
    ablation_broke_ptp: int                      # denom for catch rate
    wake_caught_regression: int                  # numer for catch rate
    regression_catch_rate: float
    wake_correct_patches: int                    # denom for FP
    wake_fired_on_correct: int                   # numer for FP
    false_positive_rate: float


def _stratum(pairs: list[InstancePair], name: str) -> StratumMetrics:
    n = len(pairs)
    paired = [p for p in pairs if p.wake and p.ablation]
    n_paired = len(paired)
    if n_paired == 0:
        return StratumMetrics(
            name=name, n=n, n_paired=0,
            wake_resolved=0, ablation_resolved=0,
            resolved_rate_wake=0.0, resolved_rate_ablation=0.0, resolved_rate_delta=0.0,
            ablation_broke_ptp=0, wake_caught_regression=0,
            regression_catch_rate=float("nan"),
            wake_correct_patches=0, wake_fired_on_correct=0,
            false_positive_rate=float("nan"),
        )
    wake_res = sum(1 for p in paired if p.wake.resolved)
    abl_res = sum(1 for p in paired if p.ablation.resolved)
    abl_intro = sum(1 for p in paired if p.ablation.broke_pass_to_pass)
    wake_caught = sum(
        1 for p in paired
        if p.ablation.broke_pass_to_pass and p.wake.wake_fired
    )
    wake_correct = sum(1 for p in paired if p.wake.resolved)
    wake_fp = sum(1 for p in paired if p.wake.resolved and p.wake.wake_fired)
    return StratumMetrics(
        name=name,
        n=n, n_paired=n_paired,
        wake_resolved=wake_res, ablation_resolved=abl_res,
        resolved_rate_wake=wake_res / n_paired,
        resolved_rate_ablation=abl_res / n_paired,
        resolved_rate_delta=(wake_res - abl_res) / n_paired,
        ablation_broke_ptp=abl_intro,
        wake_caught_regression=wake_caught,
        regression_catch_rate=wake_caught / abl_intro if abl_intro else float("nan"),
        wake_correct_patches=wake_correct,
        wake_fired_on_correct=wake_fp,
        false_positive_rate=wake_fp / wake_correct if wake_correct else float("nan"),
    )


@dataclass
class Phase8Metrics:
    strata: dict[str, StratumMetrics]
    unpaired_only_wake: list[str] = field(default_factory=list)
    unpaired_only_ablation: list[str] = field(default_factory=list)


def compute(pairs: list[InstancePair]) -> Phase8Metrics:
    audit = _load_audit_metadata()
    analyzable_ids = {
        iid for iid, r in audit.items()
        if r.get("would_catch") in ("yes", "partial")
    }
    strata = {"all": _stratum(pairs, "all")}
    if audit:
        in_audit = [p for p in pairs if p.instance_id in audit]
        analyzable = [p for p in in_audit if p.instance_id in analyzable_ids]
        non_analyzable = [p for p in in_audit if p.instance_id not in analyzable_ids]
        strata["analyzable"] = _stratum(analyzable, "analyzable")
        strata["non_analyzable"] = _stratum(non_analyzable, "non_analyzable")

    only_wake = [p.instance_id for p in pairs if p.wake and not p.ablation]
    only_abl  = [p.instance_id for p in pairs if p.ablation and not p.wake]
    return Phase8Metrics(strata=strata, unpaired_only_wake=only_wake,
                          unpaired_only_ablation=only_abl)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pct(v: float) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:.1%}"


def print_report(m: Phase8Metrics, file=None) -> None:
    def _p(*a, **kw):
        print(*a, **kw)
        if file is not None:
            print(*a, file=file, **kw)

    _p()
    _p("=" * 70)
    _p("PHASE 8 — WAKE vs ABLATION on SWE-bench Verified")
    _p("=" * 70)

    s = m.strata.get("all")
    if not s or s.n_paired == 0:
        _p("  No paired results yet.")
        if m.unpaired_only_wake or m.unpaired_only_ablation:
            _p(f"  Unpaired: wake-only={len(m.unpaired_only_wake)}  "
               f"ablation-only={len(m.unpaired_only_ablation)}")
        _p("=" * 70)
        return

    _p(f"  Paired instances:  {s.n_paired}")
    if m.unpaired_only_wake or m.unpaired_only_ablation:
        _p(f"  Unpaired (excluded from metrics): "
           f"wake-only={len(m.unpaired_only_wake)}  "
           f"ablation-only={len(m.unpaired_only_ablation)}")
    _p()
    _p("  HEADLINE — RESOLVED-RATE DELTA")
    _p(f"    Wake arm:           {s.wake_resolved}/{s.n_paired} ({_pct(s.resolved_rate_wake)})")
    _p(f"    Ablation arm:       {s.ablation_resolved}/{s.n_paired} ({_pct(s.resolved_rate_ablation)})")
    _p(f"    Delta:              {s.resolved_rate_delta:+.1%}  "
       f"{'← headline lift' if s.resolved_rate_delta > 0 else '← no lift'}")
    _p()
    _p("  REGRESSION-CATCH RATE  (held-out breaks caught)")
    _p(f"    Ablation broke a PASS_TO_PASS test: {s.ablation_broke_ptp} instances")
    _p(f"    Wake fired on those:                {s.wake_caught_regression}")
    _p(f"    Catch rate:                         {_pct(s.regression_catch_rate)}")
    _p()
    _p("  FALSE-POSITIVE RATE  (trust metric)")
    _p(f"    Wake-arm correctly-resolved patches: {s.wake_correct_patches}")
    _p(f"    Wake fired on those:                  {s.wake_fired_on_correct}")
    _p(f"    FP rate:                              {_pct(s.false_positive_rate)}")
    _p()

    if "analyzable" in m.strata or "non_analyzable" in m.strata:
        _p("  STRATIFIED  (wake's instrument applies to the analyzable subset only)")
        hdr = f"  {'subset':<18} {'N':>4}  {'wake':>9}  {'abl':>9}  {'Δ':>7}  {'FP':>6}  {'catch':>7}"
        _p(hdr)
        _p("  " + "-" * (len(hdr) - 2))
        for key in ("analyzable", "non_analyzable", "all"):
            if key not in m.strata:
                continue
            ss = m.strata[key]
            wr = f"{ss.wake_resolved}/{ss.n_paired}" if ss.n_paired else "—"
            ar = f"{ss.ablation_resolved}/{ss.n_paired}" if ss.n_paired else "—"
            _p(
                f"  {ss.name:<18} {ss.n_paired:>4}  "
                f"{wr:>9}  {ar:>9}  "
                f"{ss.resolved_rate_delta:>+7.1%}  "
                f"{_pct(ss.false_positive_rate):>6}  "
                f"{_pct(ss.regression_catch_rate):>7}"
            )
        _p()

    gate = (
        s.resolved_rate_delta > 0
        and (math.isnan(s.false_positive_rate) or s.false_positive_rate < 0.10)
        and s.n_paired >= 50
    )
    _p(f"  PHASE 8 GATE: {'PASS ✓' if gate else 'FAIL ✗'}")
    _p("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default=str(Path(__file__).parent / "results"))
    p.add_argument("--manifest", default=str(Path(__file__).parent / "reports" / "phase8_manifest.json"))
    p.add_argument("--report-out", default=str(Path(__file__).parent / "reports" / "phase8_report.txt"))
    args = p.parse_args()

    pairs = collect_pairs(Path(args.results_dir), Path(args.manifest))
    if not pairs:
        print(f"No results found under {args.results_dir} (manifest: {args.manifest})")
        sys.exit(1)

    m = compute(pairs)
    Path(args.report_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.report_out, "w") as f:
        print_report(m, file=f)
    print(f"\nReport -> {args.report_out}")


if __name__ == "__main__":
    main()
