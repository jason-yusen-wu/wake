"""
probe/oracle/harness.py — Rung 2 Wizard-of-Oz oracle CEGIS loop.

Runs the agent on a case with *perfect* pre-recorded feedback injected on
the first iteration (and re-injected on subsequent iterations if the model
doesn't fix it).  Uses the same model/API as the real harness but replaces
the wake daemon with static oracle feedback.

This measures the *ceiling* the real engine can approach:
  oracle_fix_rate - ablation_fix_rate = max achievable lift

If this delta is near zero, no engine-generated feedback will move the
needle and you've learned it without writing more Rust.  If it's positive,
the project is "close this gap" — a tractable engineering question.

The oracle loop runs in two modes per case:
  oracle   — model receives the pre-recorded oracle feedback
  ablation — model receives "try again" (no analysis information)

Usage:
  python harness.py --instance-id <id>
  python harness.py --all           # run all instances with oracle feedback
  python harness.py --all --dry-run # check which instances have feedback
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Reuse the SWE-bench partition loader from Phase 8
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "harness" / "eval"))
from partition import InstancePartition, load_dataset as load_swebench, select_instances

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

FEEDBACK_DIR = Path(__file__).parent / "feedback"
RESULTS_DIR  = Path(__file__).parent / "results"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OracleRunResult:
    instance_id: str
    arm: str                    # "oracle" or "ablation"
    success: bool               # model produced a patch that (by its own output) looks fixed
    iterations: int
    latency_ms: list[float] = field(default_factory=list)
    final_patch: str = ""
    error: str = ""


@dataclass
class OraclePairResult:
    instance_id: str
    oracle: OracleRunResult
    ablation: OracleRunResult


# ---------------------------------------------------------------------------
# Oracle loop
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a precise Python engineer. You will be given a task and the "
    "contents of a Python file. Return ONLY the complete corrected file "
    "inside a single ```python ... ``` block with no other text."
)


def _extract_python(text: str) -> str | None:
    import re
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else None


def run_oracle_arm(
    instance: InstancePartition,
    feedback_text: str | None,   # None = ablation (no analysis information)
    model: str,
    budget: int,
    client: "anthropic.Anthropic",
) -> OracleRunResult:
    """
    Run one arm of the oracle loop for a single SWE-bench instance.

    `feedback_text` is the oracle-provided finding (or None for ablation).

    We use the problem statement + the patch from the instance as the
    "current code" shown to the model.  In a real loop the model would
    have already produced some edit; here we start from the original
    problem context and let the model propose a fix.

    The success criterion is conservative: the model returned a code block
    (structural success).  Actual correctness is determined by the test
    harness — this oracle loop measures "does the model *try* to fix the
    right thing" given perfect vs no feedback.
    """
    arm = "oracle" if feedback_text else "ablation"
    latencies: list[float] = []

    # Build initial message.
    user_msg = (
        f"Task: {instance.problem_statement}\n\n"
        "Fix the reported issue in the code and return the complete corrected "
        "file in a ```python ... ``` block.\n"
    )
    if feedback_text:
        user_msg += f"\nStatic analysis finding:\n{feedback_text}\n"
    else:
        user_msg += (
            "\n(No analysis information available. Please apply your best judgment.)\n"
        )

    messages: list[dict] = [{"role": "user", "content": user_msg}]

    for iteration in range(budget):
        t0 = time.perf_counter()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=_SYSTEM,
                messages=messages,
            )
        except Exception as exc:
            return OracleRunResult(
                instance_id=instance.instance_id,
                arm=arm,
                success=False,
                iterations=iteration + 1,
                latency_ms=latencies,
                error=str(exc),
            )
        latencies.append((time.perf_counter() - t0) * 1000)
        assistant_text = response.content[0].text
        patch = _extract_python(assistant_text)

        if patch is None:
            # Model didn't return code; nudge.
            messages.append({"role": "assistant", "content": assistant_text})
            messages.append({
                "role": "user",
                "content": "Please return the complete corrected file in a ```python ... ``` block.",
            })
            continue

        # Structural success: the model returned a code block.
        # (Actual test-based correctness measured by eval.py using the saved patch.)
        return OracleRunResult(
            instance_id=instance.instance_id,
            arm=arm,
            success=True,
            iterations=iteration + 1,
            latency_ms=latencies,
            final_patch=patch,
        )

    return OracleRunResult(
        instance_id=instance.instance_id,
        arm=arm,
        success=False,
        iterations=budget,
        latency_ms=latencies,
    )


# ---------------------------------------------------------------------------
# Per-instance runner
# ---------------------------------------------------------------------------

def run_instance(
    instance: InstancePartition,
    feedback_text: str,
    model: str,
    budget: int,
    client: "anthropic.Anthropic",
    output_dir: Path,
) -> OraclePairResult:
    """Run both oracle and ablation arms for one instance."""
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [oracle]    ", end="", flush=True)
    oracle_result = run_oracle_arm(instance, feedback_text, model, budget, client)
    print("OK" if oracle_result.success else f"FAIL ({oracle_result.error[:60]})")

    print(f"  [ablation]  ", end="", flush=True)
    ablation_result = run_oracle_arm(instance, None, model, budget, client)
    print("OK" if ablation_result.success else f"FAIL ({ablation_result.error[:60]})")

    pair = OraclePairResult(
        instance_id=instance.instance_id,
        oracle=oracle_result,
        ablation=ablation_result,
    )

    # Persist result and patches.
    _save_pair(pair, output_dir)
    return pair


def _save_pair(pair: OraclePairResult, output_dir: Path) -> None:
    result_path = output_dir / f"{pair.instance_id}.json"
    with open(result_path, "w") as f:
        json.dump({
            "instance_id": pair.instance_id,
            "oracle": {
                "success": pair.oracle.success,
                "iterations": pair.oracle.iterations,
                "latency_ms": pair.oracle.latency_ms,
                "error": pair.oracle.error,
            },
            "ablation": {
                "success": pair.ablation.success,
                "iterations": pair.ablation.iterations,
                "latency_ms": pair.ablation.latency_ms,
                "error": pair.ablation.error,
            },
        }, f, indent=2)
    if pair.oracle.final_patch:
        (output_dir / f"{pair.instance_id}_oracle.py").write_text(pair.oracle.final_patch)
    if pair.ablation.final_patch:
        (output_dir / f"{pair.instance_id}_ablation.py").write_text(pair.ablation.final_patch)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Rung 2 oracle harness: measure lift ceiling with perfect feedback."
    )
    p.add_argument("--instance-id", help="run a single instance by ID")
    p.add_argument("--all", action="store_true", help="run all instances with feedback recorded")
    p.add_argument("--dry-run", action="store_true",
                   help="print which instances have oracle feedback without running the model")
    p.add_argument("--dataset", default="swebench_verified",
                   help="SWE-bench dataset path or HuggingFace name")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--budget", type=int, default=3)
    p.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = p.parse_args()

    if args.dry_run:
        feedback_files = sorted(FEEDBACK_DIR.glob("*.json"))
        print(f"Oracle feedback recorded for {len(feedback_files)} instances:")
        for f in feedback_files:
            print(f"  {f.stem}")
        return

    if not _HAS_ANTHROPIC:
        print("ERROR: anthropic package not installed.  pip install anthropic")
        sys.exit(1)

    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic()

    # Collect instances to run.
    all_instances = load_swebench(args.dataset)
    feedback_files = sorted(FEEDBACK_DIR.glob("*.json"))
    feedback_map: dict[str, str] = {}
    for ff in feedback_files:
        fb = json.loads(ff.read_text())
        feedback_map[fb["instance_id"]] = fb["feedback_text"]

    if args.instance_id:
        ids = [args.instance_id]
    elif args.all:
        ids = list(feedback_map.keys())
    else:
        p.error("Specify --instance-id <id> or --all")

    instances = select_instances(all_instances, instance_ids=ids)
    if not instances:
        print(f"No matching instances found for IDs: {ids}")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    results: list[OraclePairResult] = []

    for inst in instances:
        fb_text = feedback_map.get(inst.instance_id)
        if not fb_text:
            print(f"[{inst.instance_id}] No oracle feedback — skipping.")
            continue
        print(f"\n[{inst.instance_id}]")
        pair = run_instance(inst, fb_text, args.model, args.budget, client, output_dir)
        results.append(pair)

    if results:
        print(f"\n{len(results)} instances run.  Saved to {output_dir}")
        print("Next: python eval.py")


if __name__ == "__main__":
    main()
