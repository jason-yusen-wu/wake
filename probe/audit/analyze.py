"""
probe/audit/analyze.py — Rung 1 bucket analysis.

Answers the three decisive questions from the design doc:
  (a) What fraction of failures are in the analyzable bucket?
      (test of the strategic claim — "analysis has something to say")
  (b) Which property should be built first?
      (pick the bucket with the highest yes/partial verdict count)
  (c) Kill/redirect signals:
      • If "misunderstood_intent" dominates, analysis cannot help — STOP.
      • If "change_consistency" dominates over "null_type", redirect to
        blast-radius completeness as the first property (already computable).

The output is a structured AuditResult dataclass, printable as a report
and consumable by the oracle harness to select the analyzable subset.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from schema import (
    AnalysisProperty, AnalysisVerdict, FailureCategory, LabeledFailure,
)
import dataset as ds


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BucketStats:
    category: FailureCategory
    count: int
    analyzable: int      # yes + partial
    yes: int
    partial: int
    no: int
    unknown: int

    @property
    def analyzable_rate(self) -> float:
        return self.analyzable / self.count if self.count else 0.0


@dataclass
class AuditResult:
    n_total: int
    n_labeled: int

    # Bucket breakdown
    buckets: list[BucketStats]

    # Top-level fractions (labeled only)
    analyzable_count: int
    analyzable_rate: float   # analyzable / labeled

    # Property vote: which property had the most yes/partial verdicts
    property_votes: dict[str, int]
    recommended_property: AnalysisProperty

    # Kill/redirect signals
    misunderstood_fraction: float   # if > 0.4: project kill signal
    change_consistency_fraction: float  # if > null_type_fraction: redirect signal

    # Subset for Rung 2
    analyzable_instances: list[LabeledFailure]

    @property
    def kill_signal(self) -> bool:
        """True if 'misunderstood_intent' dominates → analysis can't help."""
        return self.misunderstood_fraction > 0.40

    @property
    def redirect_signal(self) -> bool:
        """True if change_consistency beats nullability → build blast-radius first."""
        null_votes = self.property_votes.get(AnalysisProperty.NULLABILITY.value, 0)
        cc_votes = self.property_votes.get(AnalysisProperty.CHANGE_CONSISTENCY.value, 0)
        return cc_votes > null_votes


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute(failures: list[LabeledFailure]) -> AuditResult:
    labeled = [f for f in failures if f.is_labeled]
    n_labeled = len(labeled)

    # Per-category breakdown.
    by_cat: dict[FailureCategory, list[LabeledFailure]] = {}
    for f in labeled:
        by_cat.setdefault(f.category, []).append(f)

    buckets: list[BucketStats] = []
    for cat in FailureCategory:
        if cat == FailureCategory.UNLABELED:
            continue
        items = by_cat.get(cat, [])
        if not items:
            continue
        yes    = sum(1 for f in items if f.would_catch == AnalysisVerdict.YES)
        partial = sum(1 for f in items if f.would_catch == AnalysisVerdict.PARTIAL)
        no     = sum(1 for f in items if f.would_catch == AnalysisVerdict.NO)
        unk    = sum(1 for f in items if f.would_catch == AnalysisVerdict.UNKNOWN)
        buckets.append(BucketStats(
            category=cat,
            count=len(items),
            analyzable=yes + partial,
            yes=yes, partial=partial, no=no, unknown=unk,
        ))
    buckets.sort(key=lambda b: b.count, reverse=True)

    analyzable = [f for f in labeled if f.is_analyzable]
    analyzable_rate = len(analyzable) / n_labeled if n_labeled else 0.0

    # Property votes: for each analyzable failure, count the property.
    prop_votes: Counter[str] = Counter()
    for f in analyzable:
        if f.which_property != AnalysisProperty.NONE:
            prop_votes[f.which_property.value] += 1

    recommended: AnalysisProperty = AnalysisProperty.NONE
    if prop_votes:
        top = prop_votes.most_common(1)[0][0]
        recommended = AnalysisProperty(top)

    # Kill/redirect fractions.
    misunderstood = sum(1 for f in labeled if f.category == FailureCategory.MISUNDERSTOOD_INTENT)
    null_type = sum(1 for f in analyzable if f.which_property == AnalysisProperty.NULLABILITY)
    change_con = sum(1 for f in analyzable if f.which_property == AnalysisProperty.CHANGE_CONSISTENCY)
    misunderstood_frac = misunderstood / n_labeled if n_labeled else 0.0
    cc_frac = change_con / len(analyzable) if analyzable else 0.0

    return AuditResult(
        n_total=len(failures),
        n_labeled=n_labeled,
        buckets=buckets,
        analyzable_count=len(analyzable),
        analyzable_rate=analyzable_rate,
        property_votes=dict(prop_votes),
        recommended_property=recommended,
        misunderstood_fraction=misunderstood_frac,
        change_consistency_fraction=cc_frac,
        analyzable_instances=analyzable,
    )


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

_W = 65


def print_report(r: AuditResult) -> None:
    print()
    print("=" * _W)
    print("RUNG 1 — FAILURE AUDIT REPORT")
    print("=" * _W)
    print(f"  Total records:  {r.n_total}   Labeled: {r.n_labeled}")
    if r.n_total > r.n_labeled:
        print(f"  Unlabeled:      {r.n_total - r.n_labeled}  (run label.py to continue)")
    print()
    print("  CATEGORY BREAKDOWN (labeled failures)")
    print(f"  {'Category':<24} {'Count':>5}  {'Analyzable':>10}  {'Yes':>4}  {'Partial':>7}  {'No':>4}")
    print("  " + "─" * 61)
    for b in r.buckets:
        flag = " ←" if b.category == FailureCategory.MISUNDERSTOOD_INTENT else ""
        print(
            f"  {b.category.value:<24} {b.count:>5}  "
            f"{b.analyzable:>5} ({b.analyzable_rate:>4.0%})  "
            f"{b.yes:>4}  {b.partial:>7}  {b.no:>4}{flag}"
        )
    print()
    print(f"  Analyzable bucket:  {r.analyzable_count}/{r.n_labeled} ({r.analyzable_rate:.0%})")
    print()
    print("  PROPERTY VOTES (analyzable failures only)")
    for prop, count in sorted(r.property_votes.items(), key=lambda x: -x[1]):
        marker = " ← recommended" if AnalysisProperty(prop) == r.recommended_property else ""
        print(f"    {prop:<25} {count}{marker}")
    print()
    print(f"  RECOMMENDED PROPERTY:  {r.recommended_property.value}")
    print()

    # Signals
    print("  SIGNALS")
    print("  " + "─" * 40)
    if r.kill_signal:
        print(f"  ✗ KILL:      misunderstood_intent = {r.misunderstood_fraction:.0%} (> 40%)")
        print("               Analysis cannot help with the dominant failure mode.")
        print("               Re-evaluate the premise before proceeding.")
    else:
        print(f"  ✓ No kill:   misunderstood_intent = {r.misunderstood_fraction:.0%} (≤ 40%)")

    if r.redirect_signal:
        print(f"  ↪ REDIRECT:  change_consistency ({r.property_votes.get('change_consistency', 0)}) "
              f"> nullability ({r.property_votes.get('nullability', 0)})")
        print("               Consider blast-radius / missed-caller detection as the first property.")
        print("               (wake already computes this; prioritise it in the eval.)")
    else:
        null_v = r.property_votes.get('nullability', 0)
        cc_v   = r.property_votes.get('change_consistency', 0)
        print(f"  ✓ No redirect: nullability ({null_v}) ≥ change_consistency ({cc_v})")

    print()
    print(f"  PHASE 8 SUB-POPULATION for Rung 2 oracle test: {r.analyzable_count} instances")
    print("=" * _W)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Compute Rung 1 audit metrics.")
    p.add_argument("--dataset", default=str(ds.DEFAULT_DATASET))
    args = p.parse_args()

    failures = ds.load(Path(args.dataset))
    if not failures:
        print(f"No records found in {args.dataset}. Run collect.py first.")
        return

    result = compute(failures)
    print_report(result)


if __name__ == "__main__":
    main()
