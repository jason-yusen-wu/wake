"""
probe/smoke_test.py — self-contained verification that the probe data pipeline
works end-to-end.  Requires no API key, no Docker, no SWE-bench download.

Creates synthetic LabeledFailure records, runs the full path through:
  save → load → label (programmatically) → analyze → report
  + oracle schema round-trip

Exits 0 on success, 1 on any failure.  Run before any live session.

Usage:
  python probe/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Use importlib to load modules from different subdirectories that share names.
import importlib.util, types

def _load(mod_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

ROOT = Path(__file__).parent

audit_schema  = _load("audit_schema",  ROOT / "audit" / "schema.py")
oracle_schema = _load("oracle_schema", ROOT / "oracle" / "schema.py")

# Import audit helpers with the audit_schema loaded first.
sys.path.insert(0, str(ROOT / "audit"))
import dataset as ds
import analyze as az

FailureCategory  = audit_schema.FailureCategory
AnalysisVerdict  = audit_schema.AnalysisVerdict
AnalysisProperty = audit_schema.AnalysisProperty
PatchSource      = audit_schema.PatchSource
LabeledFailure   = audit_schema.LabeledFailure
OracleFeedback   = oracle_schema.OracleFeedback

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"

errors: list[str] = []

def check(name: str, cond: bool, msg: str = "") -> None:
    if cond:
        print(f"  {_PASS} {name}")
    else:
        print(f"  {_FAIL} {name}  {msg}")
        errors.append(name)


# ---------------------------------------------------------------------------
# 1. Schema round-trip
# ---------------------------------------------------------------------------

def make_failure(instance_id: str, cat: FailureCategory, verdict: AnalysisVerdict,
                 prop: AnalysisProperty, source: PatchSource) -> LabeledFailure:
    return LabeledFailure(
        instance_id=instance_id,
        repo="owner/repo",
        base_commit="abc123",
        problem_statement=f"Fix the bug in {instance_id}.",
        patch="--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x = None\n+x = 'hello'\n",
        patch_source=source,
        fail_to_pass=["test_foo"],
        pass_to_pass=["test_bar"],
        test_results={"test_foo": "FAILED", "test_bar": "PASSED"},
        category=cat,
        would_catch=verdict,
        which_property=prop,
        analysis_note="x = None then x.attr",
        labeled_by="human",
        label_timestamp="2026-05-29T00:00:00",
    )


print("\nSmoke test — probe data pipeline\n")

# Round-trip via to_dict / from_dict
f1 = make_failure("repo__foo-1", FailureCategory.NULL_TYPE, AnalysisVerdict.YES,
                  AnalysisProperty.NULLABILITY, PatchSource.GOLD)
d = f1.to_dict()
f1b = LabeledFailure.from_dict(d)
check("LabeledFailure round-trip (to_dict/from_dict)", f1 == f1b)
check("is_labeled", f1.is_labeled)
check("is_analyzable (YES)", f1.is_analyzable)

f_no = make_failure("repo__foo-2", FailureCategory.WRONG_LOGIC, AnalysisVerdict.NO,
                    AnalysisProperty.NONE, PatchSource.AGENT_FAILED)
check("not is_analyzable (NO)", not f_no.is_analyzable)
check("broke_passing_tests empty when ptp passes", f1.broke_passing_tests == [])

# ---------------------------------------------------------------------------
# 2. Dataset JSONL round-trip
# ---------------------------------------------------------------------------

failures = [
    f1,
    f_no,
    make_failure("repo__foo-3", FailureCategory.INCOMPLETE_EDIT, AnalysisVerdict.YES,
                 AnalysisProperty.CHANGE_CONSISTENCY, PatchSource.AGENT_REGRESSED),
    make_failure("repo__foo-4", FailureCategory.MISUNDERSTOOD_INTENT, AnalysisVerdict.NO,
                 AnalysisProperty.NONE, PatchSource.AGENT_FAILED),
    make_failure("repo__foo-5", FailureCategory.NULL_TYPE, AnalysisVerdict.PARTIAL,
                 AnalysisProperty.NULLABILITY, PatchSource.GOLD),
]

with tempfile.TemporaryDirectory() as tmpdir:
    db_path = Path(tmpdir) / "test.jsonl"

    ds.save(failures, db_path)
    loaded = ds.load(db_path)
    check("dataset save/load round-trip count", len(loaded) == len(failures))
    check("dataset instance_ids preserved",
          {f.instance_id for f in loaded} == {f.instance_id for f in failures})

    # Upsert: update one record and check it replaced, not duplicated.
    f1_updated = LabeledFailure.from_dict({**f1.to_dict(), "analysis_note": "updated"})
    ds.upsert(f1_updated, db_path)
    loaded2 = ds.load(db_path)
    check("upsert replaces (no duplicate)", len(loaded2) == len(failures))
    note = next(f.analysis_note for f in loaded2 if f.instance_id == f1.instance_id)
    check("upsert updates field", note == "updated")

    # Filter
    analyzable = ds.filter_failures(loaded2, analyzable_only=True)
    check("filter analyzable_only",
          all(f.is_analyzable for f in analyzable) and len(analyzable) == 3)
    null_only = ds.filter_failures(loaded2, category=FailureCategory.NULL_TYPE)
    check("filter by category",
          all(f.category == FailureCategory.NULL_TYPE for f in null_only))

    # ---------------------------------------------------------------------------
    # 3. analyze.compute
    # ---------------------------------------------------------------------------

    result = az.compute(loaded2)
    check("analyze.n_total", result.n_total == len(failures))
    check("analyze.n_labeled == n_total (all labeled)", result.n_labeled == len(failures))
    check("analyze.analyzable_count == 3",  result.analyzable_count == 3)
    check("analyze.analyzable_rate ≈ 0.6",  abs(result.analyzable_rate - 0.6) < 0.01)
    check("property_votes has nullability",
          AnalysisProperty.NULLABILITY.value in result.property_votes)
    check("recommended_property is nullability",
          result.recommended_property == AnalysisProperty.NULLABILITY)
    check("kill_signal False (misunderstood = 1/5 = 20%)", not result.kill_signal)
    check("redirect_signal False (null 2 > cc 1)", not result.redirect_signal)

    # Report should not raise.
    try:
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            az.print_report(result)
        report_text = buf.getvalue()
        check("print_report runs without error", True)
        check("report contains RUNG 1", "RUNG 1" in report_text)
    except Exception as exc:
        check("print_report runs without error", False, str(exc))

# ---------------------------------------------------------------------------
# 4. Oracle schema round-trip
# ---------------------------------------------------------------------------

fb = OracleFeedback(
    instance_id="repo__foo-1",
    feedback_text="[HIGH] Root cause: 'x' assigned None.\n  • line 3: 'x' used as attribute.",
    category="null_type",
    which_property="nullability",
    confidence="high",
    is_analyzable=True,
    gold_patch="--- a/foo.py",
    recorded_by="human",
    record_timestamp="2026-05-29T00:00:00",
)
fb_d = fb.to_dict()
fb2 = OracleFeedback.from_dict(fb_d)
check("OracleFeedback round-trip", fb == fb2)
check("OracleFeedback feedback_text preserved",
      fb2.feedback_text == fb.feedback_text)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print()
if errors:
    print(f"\033[31m{len(errors)} check(s) failed: {', '.join(errors)}\033[0m")
    sys.exit(1)
else:
    print(f"\033[32mAll checks passed.\033[0m")
    print("\nProbe tooling is ready.")
    print("\nWorkflow:")
    print("  1. python probe/audit/collect.py --source gold --n 100")
    print("  2. python probe/audit/label.py")
    print("  3. python probe/audit/analyze.py       ← Rung 1 gate")
    print("  4. python probe/oracle/record.py       ← write oracle feedback")
    print("  5. python probe/oracle/harness.py --all")
    print("  6. python probe/oracle/eval.py         ← Rung 2 ceiling")
