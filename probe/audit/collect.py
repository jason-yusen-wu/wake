"""
probe/audit/collect.py — populate the labeled-failure dataset with unlabeled
entries ready for hand-labeling.

Two source modes:

  gold  (--source gold)
    Loads instances directly from SWE-bench Verified (HuggingFace or JSONL).
    Uses the *gold patch* as the patch to label.  Ask: "given the actual bug
    being fixed, is this the kind of mistake wake would catch if an agent
    introduced it?"  Useful for pre-run auditing when you don't have agent
    trajectories yet.

  agent (--source agent)
    Loads from a batch_eval results directory produced by task_runner.py.
    Uses the *agent's patch* (which failed held-out tests or broke passing
    ones) as the patch to label.  This is the gold-standard audit: you're
    labeling real agent failures, not hypotheticals.

In both modes, instances already in the dataset are skipped unless
--overwrite is given.

Usage:
  # Gold-patch mode: 50 instances from SWE-bench Verified
  python collect.py --source gold --n 50

  # Agent-trajectory mode: all failures from a results directory
  python collect.py --source agent --results-dir ../../harness/eval/results
"""
from __future__ import annotations

import argparse
import json
import random as _random
import sys
from pathlib import Path

from schema import LabeledFailure, PatchSource
import dataset as ds

# ---------------------------------------------------------------------------
# Gold-patch collection
# ---------------------------------------------------------------------------

def collect_gold(
    n: int,
    swebench_path: str = "swebench_verified",
    skip_ids: set[str] | None = None,
    seed: int = 42,
) -> list[LabeledFailure]:
    """
    Load `n` instances from SWE-bench Verified and create unlabeled
    LabeledFailure entries using the gold patch.

    The dataset is shuffled with a fixed seed before sampling so the selected
    instances are representative of the full repo distribution (~46% django,
    ~15% sympy, ~9% sphinx, etc.) rather than the alphabetically-biased first N.
    """
    instances = _load_swebench(swebench_path)
    # Shuffle for representative sampling.  Using a seeded RNG keeps the
    # selection reproducible: the same seed always selects the same instances.
    rng = _random.Random(seed)
    rng.shuffle(instances)
    skip = skip_ids or set()
    collected: list[LabeledFailure] = []
    for inst in instances:
        if inst["instance_id"] in skip:
            continue
        ftp = inst.get("FAIL_TO_PASS", [])
        ptp = inst.get("PASS_TO_PASS", [])
        if isinstance(ftp, str):
            ftp = json.loads(ftp)
        if isinstance(ptp, str):
            ptp = json.loads(ptp)
        collected.append(LabeledFailure(
            instance_id=inst["instance_id"],
            repo=inst.get("repo", ""),
            base_commit=inst.get("base_commit", ""),
            problem_statement=inst.get("problem_statement", ""),
            patch=inst.get("patch", ""),
            patch_source=PatchSource.GOLD,
            fail_to_pass=ftp,
            pass_to_pass=ptp,
            # test_results unknown for gold mode: we're labeling the bug type,
            # not an actual test run.
            test_results={},
        ))
        if len(collected) >= n:
            break
    return collected


# ---------------------------------------------------------------------------
# Agent-trajectory collection
# ---------------------------------------------------------------------------

def collect_agent(
    results_dir: Path,
    skip_ids: set[str] | None = None,
) -> list[LabeledFailure]:
    """
    Walk a batch_eval results directory and create LabeledFailure entries for
    instances where the ablation arm failed or introduced regressions.

    We prefer the ablation arm over the wake arm to get a picture of agent
    behaviour without the gate, which is what the audit is asking about.
    """
    skip = skip_ids or set()
    collected: list[LabeledFailure] = []

    for result_path in sorted(results_dir.glob("*/result.json")):
        data = json.loads(result_path.read_text())
        instance_id = data["instance_id"]
        if instance_id in skip:
            continue

        abl = data.get("ablation", {})
        resolved = abl.get("resolved", False)
        ftp_results: dict[str, bool] = abl.get("fail_to_pass", {})
        ptp_results: dict[str, bool] = abl.get("pass_to_pass", {})

        # Convert bool results to "PASSED"/"FAILED" strings.
        test_results: dict[str, str] = {}
        for t, passed in {**ftp_results, **ptp_results}.items():
            test_results[t] = "PASSED" if passed else "FAILED"

        # Determine source classification.
        broke_passing = any(not v for v in ptp_results.values())
        all_ftp_failed = all(not v for v in ftp_results.values()) if ftp_results else True
        if broke_passing:
            source = PatchSource.AGENT_REGRESSED
        elif resolved:
            continue  # Resolved instances are less interesting for the failure audit.
        elif all_ftp_failed:
            source = PatchSource.AGENT_FAILED
        else:
            source = PatchSource.AGENT_PARTIAL

        # Try to read the patch from the arm's output directory.
        patch_path = result_path.parent / "ablation" / "patch.diff"
        patch = patch_path.read_text() if patch_path.exists() else ""

        # Load dataset metadata if we have the SWE-bench instance.
        collected.append(LabeledFailure(
            instance_id=instance_id,
            repo=data.get("repo", ""),
            base_commit=data.get("base_commit", ""),
            problem_statement=data.get("problem_statement", ""),
            patch=patch,
            patch_source=source,
            fail_to_pass=list(ftp_results.keys()),
            pass_to_pass=list(ptp_results.keys()),
            test_results=test_results,
        ))

    return collected


# ---------------------------------------------------------------------------
# SWE-bench loader (shared)
# ---------------------------------------------------------------------------

def _load_swebench(path: str) -> list[dict]:
    p = Path(path)
    if p.exists() and p.suffix in (".jsonl", ".json"):
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]
    # HuggingFace
    try:
        from datasets import load_dataset as hf_load
        name = "princeton-nlp/SWE-bench_Verified" if path == "swebench_verified" else path
        ds_hf = hf_load(name, split="test")
        return [dict(row) for row in ds_hf]
    except ImportError:
        print("ERROR: 'datasets' not installed. Run: pip install datasets", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Collect SWE-bench failures into the labeled-failure dataset."
    )
    p.add_argument("--source", choices=["gold", "agent"], required=True,
                   help="gold: use SWE-bench gold patches; agent: use batch_eval results")
    p.add_argument("--n", type=int, default=100,
                   help="(gold mode) number of instances to collect")
    p.add_argument("--swebench", default="princeton-nlp/SWE-bench_Verified",
                   dest="dataset",
                   help="(gold mode) local .jsonl path or HuggingFace dataset name "
                        "(default: princeton-nlp/SWE-bench_Verified)")
    p.add_argument("--seed", type=int, default=42,
                   help="random seed for reproducible instance selection (default: 42)")
    p.add_argument("--results-dir", default="../../harness/eval/results",
                   help="(agent mode) path to batch_eval results directory")
    p.add_argument("--output", default=str(ds.DEFAULT_DATASET),
                   help="output JSONL path (default: corpus/labeled_failures.jsonl)")
    p.add_argument("--overwrite", action="store_true",
                   help="re-collect instances already in the dataset")
    args = p.parse_args()

    output_path = Path(args.output)
    existing = ds.load(output_path)
    skip = set() if args.overwrite else ds.ids(existing)

    print(f"Existing records: {len(existing)}.  Skipping: {len(skip)}.")

    if args.source == "gold":
        new = collect_gold(args.n, swebench_path=args.dataset, skip_ids=skip, seed=args.seed)
    else:
        new = collect_agent(Path(args.results_dir), skip_ids=skip)

    if not new:
        print("Nothing new to collect.")
        return

    all_records = existing + new
    ds.save(all_records, output_path)
    print(f"Collected {len(new)} new records. Total: {len(all_records)}. Saved to {output_path}")

    # Write a brief collection summary for review.
    report_path = Path(__file__).parent / "reports" / "collect_summary.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as rpt:
        rpt.write(f"Collection summary\n{'='*40}\n")
        rpt.write(f"Source:   {args.source}\n")
        rpt.write(f"New:      {len(new)}\n")
        rpt.write(f"Total:    {len(all_records)}\n\n")
        from collections import Counter
        repo_counts = Counter(f.repo for f in new if f.repo)
        rpt.write("Repos:\n")
        for repo, n in repo_counts.most_common(20):
            rpt.write(f"  {repo}: {n}\n")
        rpt.write("\nFirst 20 instance IDs:\n")
        for f in new[:20]:
            rpt.write(f"  {f.instance_id}  ({f.patch_source.value})\n")
    print(f"Collection summary → {report_path}")
    print(f"\nNext: python autolabel.py  (or  python label.py  for manual labeling)")


if __name__ == "__main__":
    main()
