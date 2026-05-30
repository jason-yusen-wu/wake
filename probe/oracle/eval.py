"""
probe/oracle/eval.py — compute the Rung 2 oracle lift ceiling.

Loads results from harness.py (oracle vs ablation runs) and reports:

  HEADLINE
    Patch-coverage delta — oracle avg coverage minus ablation avg coverage.
    Coverage = fraction of gold-patch additions present verbatim in the model
    output.  This is the quality signal: did the model make the right change?
    Computed over the union of completed instances (failed arms count as 0).

  SECONDARY
    Structural success rate (returned a code block at all).  Both arms
    saturate near 85%+ on most instances because "produce any Python code"
    is easy when the file is provided.  Use as a diagnostic for runs that
    hung or timed out, not as a lift signal.

  STRATIFIED
    Coverage delta broken down by which_property so you can see whether the
    lift concentrates in wake's primary property (change_consistency).

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
AUDIT_DATASET = Path(__file__).parent.parent / "audit" / "corpus" / "labeled_failures.jsonl"


@dataclass
class OracleMetrics:
    n_instances: int
    n_skipped: int          # instances where source file could not be fetched
    # Headline: patch-coverage signal (quality).
    # Computed over ALL completed instances, with failed arms counting as 0
    # coverage — so a structural failure is not silently dropped.
    oracle_avg_coverage_all: float    # avg over all n instances (fail=0)
    ablation_avg_coverage_all: float
    coverage_delta: float             # oracle - ablation
    # Coverage among successful runs only (diagnostic).
    oracle_avg_coverage_succ: float
    ablation_avg_coverage_succ: float
    # Win/loss counts on coverage (>5% delta either way).
    n_oracle_wins: int
    n_ablation_wins: int
    n_ties: int
    # Secondary: structural success (returned a code block).
    oracle_success: int
    ablation_success: int
    oracle_rate: float
    ablation_rate: float
    structural_delta: float    # oracle_rate - ablation_rate (diagnostic only)
    # Stratified by which_property.
    # property -> (n, oracle_cov_all, abl_cov_all, oracle_succ, abl_succ,
    #              oracle_wins, ablation_wins, ties)
    by_property: dict[str, tuple[int, float, float, int, int, int, int, int]]
    # Confidence breakdown — coverage delta on high-confidence instances.
    high_confidence_coverage_delta: float
    n_errors: int
    # Diagnostic surfaces.
    error_records: list[tuple[str, str]]   # (instance_id, error_message)
    n_legacy_schema: int                   # result files missing patch_coverage_score
    out_of_scope_ids: list[str]            # results that aren't in current analyzable set


_WIN_THRESHOLD = 0.05  # >5% coverage delta counts as a win for that arm


def _recompute_coverage(
    instance_id: str, arm: str, results_dir: Path, gold_patch: str | None
) -> float | None:
    """
    Recompute patch_coverage from the saved final_patch file using the
    current metric.  Returns None if the patch file or gold patch is missing,
    so the caller falls back to the stored score.

    This keeps the eval consistent across runs even when the metric changes:
    old result files saved an additions-only score; re-scoring them from the
    saved model output gives us the new deletion-aware score without rerunning.
    """
    if not gold_patch:
        return None
    patch_path = results_dir / f"{instance_id}_{arm}.py"
    if not patch_path.exists():
        return None
    # Lazy import so eval doesn't depend on the harness when the patch files
    # are absent (e.g. running eval against a different results dir).
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from harness import patch_coverage
    return patch_coverage(gold_patch, patch_path.read_text())


def _cov(arm: dict, recomputed: float | None) -> float:
    """Per-arm coverage with failures counted as 0; prefer recomputed when present."""
    if not arm.get("success"):
        return 0.0
    if recomputed is not None:
        return recomputed
    return arm.get("patch_coverage_score", 0.0)


def _load_analyzable_ids() -> set[str]:
    """Currently-analyzable IDs per labeled_failures.jsonl, for scope filtering."""
    if not AUDIT_DATASET.exists():
        return set()
    out: set[str] = set()
    with open(AUDIT_DATASET) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("would_catch") in ("yes", "partial"):
                out.add(r["instance_id"])
    return out


def compute(results_dir: Path) -> OracleMetrics:
    result_files = sorted(results_dir.glob("*.json"))
    if not result_files:
        return OracleMetrics(
            n_instances=0, n_skipped=0,
            oracle_avg_coverage_all=float("nan"),
            ablation_avg_coverage_all=float("nan"),
            coverage_delta=float("nan"),
            oracle_avg_coverage_succ=float("nan"),
            ablation_avg_coverage_succ=float("nan"),
            n_oracle_wins=0, n_ablation_wins=0, n_ties=0,
            oracle_success=0, ablation_success=0,
            oracle_rate=0.0, ablation_rate=0.0, structural_delta=0.0,
            by_property={}, high_confidence_coverage_delta=float("nan"),
            n_errors=0,
            error_records=[], n_legacy_schema=0, out_of_scope_ids=[],
        )

    # Load feedback metadata for stratification.
    # which_property comes from the feedback JSON file, not from the result file --
    # if a feedback file is later deleted or renamed, the instance silently falls
    # into the "unknown" stratum rather than raising an error.
    feedback_meta: dict[str, dict] = {}
    for ff in FEEDBACK_DIR.glob("*.json"):
        fb = json.loads(ff.read_text())
        feedback_meta[fb["instance_id"]] = fb

    records = [json.loads(rp.read_text()) for rp in result_files]
    n = len(records)

    # Diagnostic checks (do not affect headline numbers).
    analyzable_ids = _load_analyzable_ids()
    out_of_scope = sorted(
        r["instance_id"] for r in records
        if analyzable_ids and r["instance_id"] not in analyzable_ids
    )
    n_legacy = sum(
        1 for r in records
        if "patch_coverage_score" not in r.get("oracle", {})
        or "patch_coverage_score" not in r.get("ablation", {})
    )
    error_records = sorted(
        (r["instance_id"], (r.get("oracle", {}).get("error")
                            or r.get("ablation", {}).get("error", ""))[:120])
        for r in records
        if r.get("oracle", {}).get("error") or r.get("ablation", {}).get("error")
    )

    # Recompute coverage from saved patches when possible — keeps the metric
    # consistent across runs even when the metric definition has changed.
    recomputed_coverage: dict[str, tuple[float | None, float | None]] = {}
    for r in records:
        iid = r["instance_id"]
        gold = feedback_meta.get(iid, {}).get("gold_patch")
        ro = _recompute_coverage(iid, "oracle", results_dir, gold)
        ra = _recompute_coverage(iid, "ablation", results_dir, gold)
        recomputed_coverage[iid] = (ro, ra)

    # Per-instance arm-level coverage (failures count as 0).
    per_inst = []
    for r in records:
        ro, ra = recomputed_coverage.get(r["instance_id"], (None, None))
        per_inst.append((
            _cov(r.get("oracle", {}), ro),
            _cov(r.get("ablation", {}), ra),
        ))
    oracle_avg_all = sum(o for o, _ in per_inst) / n
    abl_avg_all   = sum(a for _, a in per_inst) / n
    cov_delta = oracle_avg_all - abl_avg_all

    # Coverage among successful runs only (diagnostic).
    def _stored_or_recomputed(r: dict, arm: str) -> float:
        rec = recomputed_coverage.get(r["instance_id"], (None, None))
        idx = 0 if arm == "oracle" else 1
        if rec[idx] is not None:
            return rec[idx]
        return r[arm].get("patch_coverage_score", 0.0)

    o_succ_cov = [_stored_or_recomputed(r, "oracle")
                  for r in records if r.get("oracle", {}).get("success")]
    a_succ_cov = [_stored_or_recomputed(r, "ablation")
                  for r in records if r.get("ablation", {}).get("success")]
    o_avg_succ = sum(o_succ_cov) / len(o_succ_cov) if o_succ_cov else float("nan")
    a_avg_succ = sum(a_succ_cov) / len(a_succ_cov) if a_succ_cov else float("nan")

    # Win/loss/tie counts on per-instance coverage delta.
    n_o_wins = sum(1 for o, a in per_inst if o - a >  _WIN_THRESHOLD)
    n_a_wins = sum(1 for o, a in per_inst if a - o >  _WIN_THRESHOLD)
    n_ties   = n - n_o_wins - n_a_wins

    # Secondary: structural success.
    oracle_ok   = sum(1 for r in records if r.get("oracle", {}).get("success"))
    ablation_ok = sum(1 for r in records if r.get("ablation", {}).get("success"))
    n_errors = sum(
        1 for r in records
        if r.get("oracle", {}).get("error") or r.get("ablation", {}).get("error")
    )

    # Stratify by property: coverage + structural + wins per stratum.
    by_prop: dict[str, list[tuple[dict, float, float]]] = {}
    for r, (oc, ac) in zip(records, per_inst):
        prop = feedback_meta.get(r["instance_id"], {}).get("which_property", "unknown")
        by_prop.setdefault(prop, []).append((r, oc, ac))

    by_property: dict[str, tuple[int, float, float, int, int, int, int, int]] = {}
    for prop, items in by_prop.items():
        k = len(items)
        o_cov = sum(oc for _, oc, _ in items) / k
        a_cov = sum(ac for _, _, ac in items) / k
        ok_o = sum(1 for r, _, _ in items if r.get("oracle", {}).get("success"))
        ok_a = sum(1 for r, _, _ in items if r.get("ablation", {}).get("success"))
        wins_o = sum(1 for _, oc, ac in items if oc - ac >  _WIN_THRESHOLD)
        wins_a = sum(1 for _, oc, ac in items if ac - oc >  _WIN_THRESHOLD)
        ties   = k - wins_o - wins_a
        by_property[prop] = (k, o_cov, a_cov, ok_o, ok_a, wins_o, wins_a, ties)

    # High-confidence subset coverage delta.
    high = [
        (oc, ac) for r, (oc, ac) in zip(records, per_inst)
        if feedback_meta.get(r["instance_id"], {}).get("confidence") == "high"
    ]
    h_delta = float("nan")
    if high:
        h_o = sum(o for o, _ in high) / len(high)
        h_a = sum(a for _, a in high) / len(high)
        h_delta = h_o - h_a

    return OracleMetrics(
        n_instances=n,
        n_skipped=0,
        oracle_avg_coverage_all=oracle_avg_all,
        ablation_avg_coverage_all=abl_avg_all,
        coverage_delta=cov_delta,
        oracle_avg_coverage_succ=o_avg_succ,
        ablation_avg_coverage_succ=a_avg_succ,
        n_oracle_wins=n_o_wins,
        n_ablation_wins=n_a_wins,
        n_ties=n_ties,
        oracle_success=oracle_ok,
        ablation_success=ablation_ok,
        oracle_rate=oracle_ok / n,
        ablation_rate=ablation_ok / n,
        structural_delta=(oracle_ok - ablation_ok) / n,
        by_property=by_property,
        high_confidence_coverage_delta=h_delta,
        n_errors=n_errors,
        error_records=error_records,
        n_legacy_schema=n_legacy,
        out_of_scope_ids=out_of_scope,
    )


def _pct(v: float) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:+.1%}" if v != 0 else "0.0%"


def print_report(m: OracleMetrics, file=None) -> None:
    """Print the Rung 2 report.  If *file* is given, write to both stdout and file."""
    W = 65

    def _p(*args, **kwargs):
        print(*args, **kwargs)
        if file is not None:
            kwargs.pop("file", None)
            print(*args, file=file, **kwargs)

    _p()
    _p("=" * W)
    _p("RUNG 2 — ORACLE LIFT CEILING")
    _p("=" * W)
    _p(f"  Instances run:  {m.n_instances}   Errors: {m.n_errors}")
    _p(f"  (instances skipped for unfetchable source are reported by harness.py)")
    _p()
    _p("  HEADLINE — PATCH-COVERAGE LIFT")
    _p("  (coverage = fraction of gold-patch additions present in model output;")
    _p("   failed arms count as 0; quality signal, not 'returned any code')")
    if math.isnan(m.coverage_delta):
        _p("    No data.")
    else:
        _p(f"    Oracle avg coverage:    {m.oracle_avg_coverage_all:.1%}")
        _p(f"    Ablation avg coverage:  {m.ablation_avg_coverage_all:.1%}")
        _p(f"    Coverage delta:         {_pct(m.coverage_delta)}   <- headline lift")
        _p(f"    Per-instance wins:      oracle={m.n_oracle_wins}   "
           f"ablation={m.n_ablation_wins}   tie={m.n_ties}  "
           f"(>{_WIN_THRESHOLD:.0%} delta = win)")
        if not math.isnan(m.high_confidence_coverage_delta):
            _p(f"    High-confidence subset: {_pct(m.high_confidence_coverage_delta)}")
    _p()
    _p("  SECONDARY — STRUCTURAL SUCCESS  (returned a code block at all)")
    _p("  (both arms typically saturate; treat as a hang/timeout diagnostic)")
    _p(f"    Oracle structural:      {m.oracle_success}/{m.n_instances} ({m.oracle_rate:.1%})")
    _p(f"    Ablation structural:    {m.ablation_success}/{m.n_instances} ({m.ablation_rate:.1%})")
    _p(f"    Structural delta:       {_pct(m.structural_delta)}")
    if not (math.isnan(m.oracle_avg_coverage_succ) or math.isnan(m.ablation_avg_coverage_succ)):
        _p(f"    Avg coverage | success: oracle={m.oracle_avg_coverage_succ:.1%}  "
           f"ablation={m.ablation_avg_coverage_succ:.1%}")
    _p()
    if m.by_property:
        _p("  STRATIFIED BY PROPERTY  (Δ cov is the lift; wins are O/A/tie on coverage)")
        _p(f"  {'Property':<22} {'N':>3}  {'O cov':>6}  {'A cov':>6}  {'Δ cov':>7}  "
           f"{'wins (O/A/=)':>12}  {'O str':>5}  {'A str':>5}")
        _p("  " + "-" * 78)
        for prop, (k, o_cov, a_cov, ok_o, ok_a, wo, wa, tie) in sorted(
            m.by_property.items(), key=lambda x: -x[1][0]
        ):
            d = o_cov - a_cov
            wins_str = f"{wo}/{wa}/{tie}"
            _p(f"  {prop:<22} {k:>3}  {o_cov:>5.0%}   {a_cov:>5.0%}   {_pct(d):>7}  "
               f"{wins_str:>12}  {ok_o:>2}/{k:<2}  {ok_a:>2}/{k:<2}")
        _p()

    if m.error_records:
        _p(f"  ERRORS ({len(m.error_records)} instances)")
        for iid, msg in m.error_records[:6]:
            _p(f"    {iid:<48s} {msg}")
        if len(m.error_records) > 6:
            _p(f"    ... and {len(m.error_records) - 6} more")
        _p()

    notes: list[str] = []
    if m.n_legacy_schema:
        notes.append(
            f"{m.n_legacy_schema} result file(s) lack patch_coverage_score "
            f"(legacy schema, treated as 0)"
        )
    if m.out_of_scope_ids:
        notes.append(
            f"{len(m.out_of_scope_ids)} result(s) are not in current "
            f"labeled_failures analyzable set: "
            f"{', '.join(m.out_of_scope_ids[:3])}"
            f"{', ...' if len(m.out_of_scope_ids) > 3 else ''}"
        )
    if notes:
        _p("  DATA-HYGIENE NOTES")
        for n in notes:
            _p(f"    • {n}")
        _p()

    _p("  INTERPRETATION  (gated on coverage delta)")
    _p("  " + "-" * 40)
    if m.n_instances == 0:
        _p("  No results yet.  Run harness.py first.")
    elif m.coverage_delta <= 0:
        _p(f"  Zero or negative coverage delta ({_pct(m.coverage_delta)}).")
        _p("    Perfect feedback does not lift patch quality.")
        _p("    The premise is challenged: reconsider before investing in the engine.")
    elif m.coverage_delta < 0.10:
        _p(f"  Small coverage delta ({_pct(m.coverage_delta)}).")
        _p("    Analysis feedback provides marginal lift.  Proceed cautiously;")
        _p("    consider widening n before committing to Phase 8.")
    else:
        _p(f"  Positive coverage delta ({_pct(m.coverage_delta)}).")
        _p("    Perfect feedback lifts patch quality meaningfully.")
        _p("    The project is now: 'how close can automated analysis get to this ceiling?'")
        _p(f"    Phase 8 baseline to beat (oracle avg coverage): {m.oracle_avg_coverage_all:.1%}")
    _p("=" * W)


def main() -> None:
    p = argparse.ArgumentParser(description="Compute Rung 2 oracle lift ceiling.")
    p.add_argument("--results-dir", default=str(RESULTS_DIR))
    args = p.parse_args()

    m = compute(Path(args.results_dir))

    report_path = Path(__file__).parent / "reports" / "rung2_report.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as rf:
        print_report(m, file=rf)

    print(f"\nRung 2 report -> {report_path}")


if __name__ == "__main__":
    main()
