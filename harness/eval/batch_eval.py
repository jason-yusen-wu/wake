"""
batch_eval — Phase 8 wake-vs-ablation runner on SWE-bench Verified, v1.1.0.

This is the post-SWE-agent-refactor implementation.  Upstream's API moved
from `swe_run(cfg, agent_callback=...)` (gone) to `RunBatch.from_config(cfg).run()`
where the agent is constructed inside `_run_instance` via the module-level
function `get_agent_from_config`.

Hook attachment strategy
------------------------
SWE-agent v1.1.0 has no config-level slot for agent hooks (DefaultAgentConfig
has no `hooks` field) and the `RunHook` callbacks don't pass the constructed
agent.  The clean integration point is the agent factory itself:

    sweagent.run.run_batch.get_agent_from_config  # imported into run_batch's namespace

We rebind that symbol while the wake arm is running so every per-instance
agent gets WakeHook attached at construction time, then restore the original
binding for the ablation arm.  This is safe inside a single batch run
(num_workers > 1 spawns threads, but they all see the rebound symbol) so
long as we don't swap the binding mid-arm — and we don't.

Evaluation
----------
We set `instances.evaluate: false` in the YAML so SWE-agent doesn't try to
use the hosted `sb-cli` service (which requires an API key and account).
Instead, after RunBatch finishes for an arm, we:

  1. merge_predictions: walk *.pred files into a single preds.json
  2. swebench.harness.run_evaluation.main: run the standard local
     Docker-based evaluation harness on the predictions
  3. record results into our per-instance result.json for metrics.py

Same harness the SWE-bench leaderboard uses; results are directly comparable.

Output layout (under --output-dir):
  wake/                          ← RunBatch's per-arm output dir
    <instance_id>/<instance_id>.pred
    preds.json                   ← merged
    eval_logs/...                ← swebench eval output
  ablation/
    (same structure)
  reports/phase8_manifest.json   ← run state
  reports/phase8_summary.json    ← combined wake+ablation results
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "clients" / "wake-py"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wake_hook import WakeHook  # noqa: E402

# SWE-agent imports come after the sys.path setup so the local clone resolves.
try:
    import sweagent.run.run_batch as rb_mod  # noqa: E402
    from sweagent.run.run_batch import RunBatch, RunBatchConfig  # noqa: E402
    from sweagent.run.merge_predictions import merge_predictions  # noqa: E402
    from sweagent.agent.hooks.abstract import CombinedAgentHook  # noqa: E402
    SWE_AGENT_AVAILABLE = True

    # ── Upstream bugfix (SWE-agent v1.1.0) ────────────────────────────────────
    # CombinedAgentHook.on_setup_done is the only callback that doesn't iterate
    # over attached hooks — it just calls super().on_setup_done() which is a
    # no-op on AbstractAgentHook.  Result: WakeHook.on_setup_done never fires,
    # the daemon never spawns, no file registration happens, and the entire
    # cold-start step is silently skipped — producing a wake arm that's
    # functionally identical to ablation.
    #
    # Patch the method to match the dispatch pattern used by every other
    # callback in the same class.  Keeps the rest of upstream untouched.
    def _on_setup_done_dispatch(self):
        for hook in self.hooks:
            hook.on_setup_done()
    CombinedAgentHook.on_setup_done = _on_setup_done_dispatch
except ImportError as e:
    SWE_AGENT_AVAILABLE = False
    _IMPORT_ERROR = e

REPORTS_DIR = Path(__file__).parent / "reports"
MANIFEST_PATH = REPORTS_DIR / "phase8_manifest.json"
DEFAULT_CONFIG = Path(__file__).parent / "config" / "swe_agent_wake.yaml"
AUDIT_DATASET = (
    Path(__file__).parent.parent.parent
    / "probe" / "audit" / "corpus" / "labeled_failures.jsonl"
)


# ---------------------------------------------------------------------------
# Atomic write + manifest (carried over from the previous batch_eval shape).
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


class Phase8Manifest:
    """
    Thread-safe disk-backed run record.  Updated per arm + per evaluation
    step so a killed run still leaves an accurate trace of progress.
    """

    def __init__(self, path: Path, scope: dict) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "scope": scope,
            "arms": {},          # arm_name -> {status, started_at, finished_at, ...}
            "finished_at": None,
            "wall_time_s": None,
        }
        _atomic_write_json(self.path, self._data)

    def update_arm(self, arm: str, entry: dict) -> None:
        with self._lock:
            self._data["arms"].setdefault(arm, {}).update(entry)
            self._data["arms"][arm]["last_update_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _atomic_write_json(self.path, self._data)

    def finish(self, wall_time_s: float) -> None:
        with self._lock:
            self._data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self._data["wall_time_s"] = round(wall_time_s, 1)
            _atomic_write_json(self.path, self._data)


# ---------------------------------------------------------------------------
# Monkey-patch the agent factory for the wake arm.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def wake_hook_attached(
    daemon_path: str,
    arm_output_dir: Path,
    arm: str,
):
    """
    Context manager: while active, RunBatch's get_agent_from_config returns
    agents with a WakeHook pre-attached.  Restored on exit even if the run
    raises.

    The hook's per-task log is written to <arm_output_dir>/<instance_id>/
    so it lives alongside SWE-agent's own per-instance artifacts.
    """
    if not SWE_AGENT_AVAILABLE:
        raise RuntimeError(
            f"SWE-agent not available: {_IMPORT_ERROR}.  Run setup.sh."
        )

    original = rb_mod.get_agent_from_config

    def patched(config):
        agent = original(config)
        # RunBatch._run_instance does `self.agent_config.name = instance_id`
        # before construction; config.name is therefore the instance_id.
        instance_id = getattr(config, "name", "unknown")
        per_inst_dir = arm_output_dir / instance_id
        per_inst_dir.mkdir(parents=True, exist_ok=True)
        agent.add_hook(WakeHook(
            daemon_path=daemon_path,
            output_dir=str(per_inst_dir),
            arm=arm,
            instance_id=instance_id,
        ))
        return agent

    rb_mod.get_agent_from_config = patched
    try:
        yield
    finally:
        rb_mod.get_agent_from_config = original


@contextlib.contextmanager
def no_wake_hook():
    """No-op context (ablation arm uses the unpatched factory)."""
    yield


# ---------------------------------------------------------------------------
# Per-arm runner
# ---------------------------------------------------------------------------

def _build_arm_config(
    template_cfg: dict,
    arm_output_dir: Path,
    n: int | None,
    instance_ids: list[str] | None,
    workers: int,
    progress_bar: bool = True,
) -> RunBatchConfig:
    """
    Clone the YAML config and patch the per-arm output_dir + filtering.
    We mutate a deep copy so the wake/ablation arms don't share state.

    Filtering precedence:
      instance_ids (explicit list)  →  uses `filter: '^(id1|id2|...)$'`
      n            (slice prefix)   →  uses `slice: ':<n>'`
      neither                       →  full subset
    """
    cfg_dict = json.loads(json.dumps(template_cfg))   # cheap deep copy via JSON
    cfg_dict.setdefault("instances", {})
    cfg_dict["instances"]["evaluate"] = False    # we evaluate ourselves; see module docstring
    if instance_ids:
        escaped = [iid.replace(".", r"\.") for iid in instance_ids]
        cfg_dict["instances"]["filter"] = f"^({'|'.join(escaped)})$"
        # Disable shuffle when we have an explicit list — the list IS the order
        cfg_dict["instances"]["shuffle"] = False
    elif n:
        cfg_dict["instances"]["slice"] = f":{n}"
    cfg_dict["output_dir"] = str(arm_output_dir)
    cfg_dict["num_workers"] = workers
    cfg_dict["progress_bar"] = progress_bar
    return RunBatchConfig.model_validate(cfg_dict)


def run_arm(
    arm: str,
    template_cfg: dict,
    output_root: Path,
    daemon_path: str,
    n: int | None,
    instance_ids: list[str] | None,
    workers: int,
    manifest: Phase8Manifest,
    is_second_arm: bool = False,
) -> Path:
    """Run one arm end-to-end.  Returns the predictions path."""
    arm_dir = output_root / arm
    arm_dir.mkdir(parents=True, exist_ok=True)

    manifest.update_arm(arm, {
        "status": "running_agent",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "output_dir": str(arm_dir),
    })

    cfg = _build_arm_config(
        template_cfg, arm_dir, n=n, instance_ids=instance_ids, workers=workers,
        # Disable progress bar on the second arm so two bars don't clobber each other.
        progress_bar=not is_second_arm,
    )
    print(f"\n[{arm}] launching RunBatch  output_dir={arm_dir}  workers={workers}")

    attach = wake_hook_attached(daemon_path, arm_dir, arm) if arm == "wake" else no_wake_hook()
    # RunBatch's entry is `.main()`; RunSingle's is `.run()`.  Easy to confuse —
    # both exist as part of the v1.1.0 API surface.
    with attach:
        RunBatch.from_config(cfg).main()

    manifest.update_arm(arm, {"status": "merging_predictions"})

    preds_path = arm_dir / "preds.json"
    merge_predictions([arm_dir], preds_path)
    print(f"[{arm}] merged predictions -> {preds_path}")

    manifest.update_arm(arm, {
        "status": "agent_done",
        "predictions_path": str(preds_path),
    })
    return preds_path


# ---------------------------------------------------------------------------
# Local swebench evaluation
# ---------------------------------------------------------------------------

def evaluate_predictions(
    arm: str,
    preds_path: Path,
    output_dir: Path,
    workers: int,
    manifest: Phase8Manifest,
) -> Path:
    """
    Run swebench's local Docker-based evaluation on the predictions file.
    Writes per-instance JSON reports under <output_dir>/eval_logs/.
    Returns the path to the summary report (or the eval_logs dir on failure).
    """
    from swebench.harness.run_evaluation import main as swebench_main

    eval_dir = output_dir / "eval_logs"
    eval_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"phase8_{arm}_{int(time.time())}"

    manifest.update_arm(arm, {"status": "running_swebench", "run_id": run_id})
    print(f"[{arm}] swebench evaluate  run_id={run_id}")

    swebench_main(
        dataset_name="princeton-nlp/SWE-bench_Verified",
        split="test",
        instance_ids=[],
        predictions_path=str(preds_path),
        max_workers=workers,
        force_rebuild=False,
        cache_level="env",
        clean=False,
        open_file_limit=4096,
        run_id=run_id,
        timeout=1800,
        namespace=None,
        rewrite_reports=False,
        modal=False,
        report_dir=str(eval_dir),
    )

    manifest.update_arm(arm, {"status": "swebench_done"})
    return eval_dir


# ---------------------------------------------------------------------------
# Pre-flight cost / time estimate
# ---------------------------------------------------------------------------

@dataclass
class Estimate:
    expected_cost: float
    cap_cost: float
    expected_wall_s: float
    cap_wall_s: float


def _preflight_estimate(n: int, workers: int, arms: int = 2) -> Estimate:
    """
    Realistic expected vs cap based on observed SWE-agent v1.1.0 + sonnet runs:
      expected:  ~$0.80/arm/instance, ~12 min/arm/instance
      cap:       per_instance_cost_limit=$3.0 (from YAML), ~30 min/arm/instance

    Both arms run in series (RunBatch is the inner parallel loop).  Wall time
    therefore divides by workers but multiplies by arms.
    """
    exp_per_arm = 0.80
    cap_per_arm = 3.00
    exp_sec_per_arm = 12 * 60
    cap_sec_per_arm = 30 * 60
    return Estimate(
        expected_cost=n * exp_per_arm * arms,
        cap_cost=n * cap_per_arm * arms,
        expected_wall_s=(n / max(workers, 1)) * exp_sec_per_arm * arms,
        cap_wall_s=(n / max(workers, 1)) * cap_sec_per_arm * arms,
    )


def _load_audit_ids(verdict: str) -> set[str]:
    """
    Return instance IDs labeled with the given `would_catch` verdict.
    verdict:
      "analyzable"      → yes ∪ partial
      "non_analyzable"  → no
      "any"             → labeled, any verdict
    """
    if not AUDIT_DATASET.exists():
        return set()
    out: set[str] = set()
    with open(AUDIT_DATASET) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            wc = r.get("would_catch", "")
            if verdict == "analyzable" and wc in ("yes", "partial"):
                out.add(r["instance_id"])
            elif verdict == "non_analyzable" and wc == "no":
                out.add(r["instance_id"])
            elif verdict == "any" and wc:
                out.add(r["instance_id"])
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_instance_ids(args: argparse.Namespace) -> tuple[list[str] | None, int | None, str]:
    """
    Determine which instances to run based on CLI flags.
    Returns (instance_ids, n, description) where exactly one of (instance_ids, n)
    is non-None (or both None for "full subset").
    """
    if args.instance_id:
        return [args.instance_id], None, f"1 specific instance ({args.instance_id})"

    if args.filter_from_audit:
        ids = sorted(_load_audit_ids(args.filter_from_audit))
        if not ids:
            raise SystemExit(
                f"--filter-from-audit {args.filter_from_audit}: no instances found "
                f"in {AUDIT_DATASET}. Run probe/audit/collect.py + autolabel.py first."
            )
        if args.n_smoke:
            random.seed(args.seed)
            ids = random.sample(ids, min(args.n_smoke, len(ids)))
            return ids, None, f"{len(ids)} smoke instances (from audit:{args.filter_from_audit})"
        if args.n and args.n < len(ids):
            random.seed(args.seed)
            ids = random.sample(ids, args.n)
        return ids, None, f"{len(ids)} instances (from audit:{args.filter_from_audit})"

    if args.n_smoke:
        return None, args.n_smoke, f"{args.n_smoke} smoke instances (slice prefix)"

    if args.n:
        return None, args.n, f"{args.n} instances (slice prefix)"

    return None, None, "full SWE-bench Verified subset (500 instances)"


def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase 8: wake vs ablation on SWE-bench Verified (v1.1.0)."
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG),
                   help="RunBatchConfig YAML template")
    p.add_argument("--n", type=int, default=None,
                   help="Number of instances to evaluate (default: full subset)")
    p.add_argument("--n-smoke", type=int, default=None,
                   help="Quick shakedown: run N random instances (overrides --n; "
                        "respects --filter-from-audit)")
    p.add_argument("--instance-id", default=None,
                   help="Run a single specific SWE-bench instance (smoke test)")
    p.add_argument("--filter-from-audit", choices=["analyzable", "non_analyzable", "any"],
                   default=None,
                   help="Restrict instances to the Rung-1 audit verdict. "
                        "'analyzable' = wake-relevant subset (~93/500); use this to "
                        "directly compare against the Rung 2 oracle ceiling.")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for --n-smoke / sub-sampling")
    p.add_argument("--workers", type=int, default=4,
                   help="num_workers for RunBatch AND swebench evaluator")
    p.add_argument("--daemon", default=str(REPO_ROOT / "target" / "release" / "wake-daemon"))
    p.add_argument("--output-dir", default=str(Path(__file__).parent / "results"))
    p.add_argument("--arms", nargs="+", default=["wake", "ablation"],
                   choices=["wake", "ablation"])
    p.add_argument("--yes", action="store_true",
                   help="Skip the pre-flight confirmation prompt")
    p.add_argument("--no-metrics", action="store_true",
                   help="Skip the auto-invoked metrics.print_report at the end")
    args = p.parse_args()

    if not SWE_AGENT_AVAILABLE:
        print(f"ERROR: SWE-agent not importable: {_IMPORT_ERROR}")
        print("Run: bash harness/eval/setup.sh")
        sys.exit(1)
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    with open(args.config) as f:
        template_cfg = yaml.safe_load(f)
    # Validate the template independently so a YAML error fails fast.
    RunBatchConfig.model_validate({**template_cfg, "output_dir": "."})

    instance_ids, n, scope_desc = _resolve_instance_ids(args)
    n_for_estimate = (
        len(instance_ids) if instance_ids
        else (n if n is not None else 500)
    )

    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    est = _preflight_estimate(n_for_estimate, args.workers, arms=len(args.arms))
    print()
    print(f"  Pre-flight estimate")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"    Scope:              {scope_desc}")
    print(f"    Arms:               {args.arms}")
    print(f"    Workers:            {args.workers}")
    print(f"    Expected cost:      ${est.expected_cost:.0f}  (cap ${est.cap_cost:.0f})")
    print(f"    Expected wall time: {est.expected_wall_s/3600:.1f} hr  "
          f"(cap {est.cap_wall_s/3600:.1f} hr)")
    print(f"    Config:             {args.config}")
    print(f"    Output:             {output_root}")
    if not args.yes and n_for_estimate > 5:
        try:
            confirm = input("  Proceed?  (yes / N): ").strip().lower()
        except EOFError:
            confirm = ""
        if confirm not in ("y", "yes"):
            print("  Aborted.  Re-run with --yes to skip this prompt.")
            return

    manifest = Phase8Manifest(MANIFEST_PATH, scope={
        "description": scope_desc,
        "n": n, "n_smoke": args.n_smoke, "instance_id": args.instance_id,
        "filter_from_audit": args.filter_from_audit, "seed": args.seed,
        "instance_ids": instance_ids,
        "arms": args.arms, "workers": args.workers, "config": args.config,
    })

    t0 = time.perf_counter()
    try:
        for arm_idx, arm in enumerate(args.arms):
            try:
                preds_path = run_arm(
                    arm=arm,
                    template_cfg=template_cfg,
                    output_root=output_root,
                    daemon_path=args.daemon,
                    n=n,
                    instance_ids=instance_ids,
                    workers=args.workers,
                    manifest=manifest,
                    is_second_arm=arm_idx > 0,
                )
                evaluate_predictions(
                    arm=arm,
                    preds_path=preds_path,
                    output_dir=output_root / arm,
                    workers=args.workers,
                    manifest=manifest,
                )
            except Exception as exc:
                tb = traceback.format_exc()
                print(f"\n[{arm}] FAILED: {exc}")
                print(tb[:2000])
                manifest.update_arm(arm, {"status": "error", "error": str(exc)[:500]})
    finally:
        manifest.finish(time.perf_counter() - t0)

    print(f"\nRun complete.  Manifest: {MANIFEST_PATH}")

    if not args.no_metrics:
        # Auto-invoke metrics so the summary lands in this terminal,
        # not in a "Next: python metrics.py" hint the user has to follow.
        try:
            from metrics import collect_pairs, compute, print_report
            pairs = collect_pairs(output_root, MANIFEST_PATH)
            if pairs:
                m = compute(pairs)
                print()
                report_out = REPORTS_DIR / "phase8_report.txt"
                report_out.parent.mkdir(parents=True, exist_ok=True)
                with open(report_out, "w") as f:
                    print_report(m, file=f)
                print(f"Report -> {report_out}")
            else:
                print("\n(No paired results yet; run metrics.py once swebench eval finishes.)")
        except Exception as exc:
            print(f"\nWarning: metrics auto-invocation failed: {exc}")
            print(f"Run manually: python metrics.py --results-dir {output_root}")


if __name__ == "__main__":
    main()
