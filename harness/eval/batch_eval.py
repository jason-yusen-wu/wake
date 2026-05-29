"""
batch_eval — run N SWE-bench instances through both arms, collect results.

Usage:
  python batch_eval.py --n 50 --dataset swebench_verified \
      --daemon ../../target/release/wake-daemon \
      --output-dir ./results --workers 4

Results are written as per-instance JSON files in output_dir. Partial results
are preserved if the run is interrupted — already-completed instances are
skipped on re-run.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path

from partition import load_dataset, select_instances, InstancePartition
from task_runner import run_instance, InstanceResult, _arm_to_dict, _load_result


def already_done(instance_id: str, output_dir: Path) -> bool:
    result_path = output_dir / instance_id / "result.json"
    return result_path.exists()


def run_batch(
    instances: list[InstancePartition],
    daemon_path: str,
    output_dir: Path,
    model: str,
    budget: int,
    workers: int,
    arms: list[str],
) -> list[InstanceResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    todo = [i for i in instances if not already_done(i.instance_id, output_dir)]
    done_count = len(instances) - len(todo)
    if done_count:
        print(f"Skipping {done_count} already-completed instances.")

    results: list[InstanceResult] = []

    # Load already-completed results
    for inst in instances:
        path = output_dir / inst.instance_id / "result.json"
        if path.exists():
            results.append(_load_result(path))

    def _run_one(instance: InstancePartition) -> InstanceResult:
        print(f"\n[{instance.instance_id}]")
        return run_instance(
            instance=instance,
            daemon_path=daemon_path,
            output_dir=output_dir,
            model=model,
            budget=budget,
            arms=arms,
        )

    if workers == 1:
        for inst in todo:
            results.append(_run_one(inst))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_one, inst): inst for inst in todo}
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    inst = futures[future]
                    print(f"  [{inst.instance_id}] FAILED: {exc}")

    return results



if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default="swebench_verified")
    p.add_argument("--n", type=int, default=50, help="Number of instances to evaluate")
    p.add_argument("--instance-ids", nargs="*", help="Explicit instance IDs (overrides --n)")
    p.add_argument("--repo-filter", help="Only instances from repos matching this string")
    p.add_argument("--daemon", default="../../target/release/wake-daemon")
    p.add_argument("--output-dir", default="./results")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--budget", type=int, default=5)
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel workers (each worker runs one instance at a time)")
    p.add_argument("--arms", nargs="+", default=["wake", "ablation"])
    args = p.parse_args()

    print(f"Loading dataset: {args.dataset}")
    instances = load_dataset(args.dataset)
    selected = select_instances(
        instances,
        n=args.n if not args.instance_ids else None,
        instance_ids=args.instance_ids,
        repo_filter=args.repo_filter,
    )
    print(f"Selected {len(selected)} instances, arms: {args.arms}")

    results = run_batch(
        instances=selected,
        daemon_path=args.daemon,
        output_dir=Path(args.output_dir),
        model=args.model,
        budget=args.budget,
        workers=args.workers,
        arms=args.arms,
    )

    print(f"\nCompleted {len(results)} instances. Run metrics.py for summary.")
