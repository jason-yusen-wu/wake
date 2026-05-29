"""
task_runner — run one SWE-bench instance through SWE-agent, both arms.

For each instance:
  1. Wake arm:      SWE-agent + WakeHook (feedback enabled)
  2. Ablation arm:  SWE-agent + WakeHook (hook active, feedback suppressed)

Both arms use identical model, budget, and config — the only difference is
whether wake feedback appears in the agent's observation stream.

After each arm, the patch is extracted and evaluated with the SWE-bench
harness (runs tests inside Docker). Results written to output_dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from partition import InstancePartition
from wake_hook import WakeHook

# SWE-agent entry point
try:
    from sweagent.run.run_single import RunSingleConfig, run as swe_run
    from sweagent.agent.agents import DefaultAgent
    SWE_AGENT_AVAILABLE = True
except ImportError:
    SWE_AGENT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ArmResult:
    arm: str                    # "wake" or "ablation"
    instance_id: str
    patch: str                  # the patch the agent produced
    resolved: bool              # did all FAIL_TO_PASS pass + PASS_TO_PASS hold?
    fail_to_pass_results: dict[str, bool] = field(default_factory=dict)
    pass_to_pass_results: dict[str, bool] = field(default_factory=dict)
    wake_fired: bool = False    # did wake flag anything during the run?
    wake_findings_path: str = ""
    error: str = ""


@dataclass
class InstanceResult:
    instance_id: str
    wake: ArmResult
    ablation: ArmResult


# ---------------------------------------------------------------------------
# SWE-agent runner
# ---------------------------------------------------------------------------

def _run_swe_agent(
    instance: InstancePartition,
    arm: str,
    daemon_path: str,
    output_dir: Path,
    model: str,
    budget: int,
    config_path: str,
) -> tuple[str, str]:
    """
    Run SWE-agent on one instance with the WakeHook attached.
    Returns (patch_text, error_message).
    """
    arm_output = output_dir / arm
    arm_output.mkdir(parents=True, exist_ok=True)

    hook = WakeHook(
        daemon_path=daemon_path,
        output_dir=str(arm_output),
        arm=arm,
        instance_id=instance.instance_id,
    )

    # Write problem statement to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(instance.problem_statement)
        problem_path = f.name

    try:
        if SWE_AGENT_AVAILABLE:
            patch = _run_via_api(
                instance, hook, model, budget, config_path, arm_output, problem_path
            )
        else:
            patch = _run_via_subprocess(
                instance, arm, daemon_path, model, budget, config_path,
                arm_output, problem_path
            )
        return patch, ""
    except Exception as exc:
        return "", str(exc)
    finally:
        os.unlink(problem_path)


def _run_via_api(
    instance: InstancePartition,
    hook: WakeHook,
    model: str,
    budget: int,
    config_path: str,
    output_dir: Path,
    problem_path: str,
) -> str:
    """Run SWE-agent using its Python API and attach WakeHook directly."""
    from sweagent.run.run_single import RunSingleConfig, run as swe_run
    import yaml

    with open(config_path) as f:
        config_dict = yaml.safe_load(f)

    config_dict.setdefault("agent", {}).setdefault("model", {})["name"] = model
    config_dict["agent"]["model"]["per_instance_cost_limit"] = budget * 0.5
    config_dict.setdefault("env", {}).setdefault("repo", {})
    config_dict["env"]["repo"]["repo_name"] = instance.repo
    config_dict["env"]["repo"]["base_commit"] = instance.base_commit
    config_dict["problem_statement"] = {"text": instance.problem_statement}
    config_dict["output_dir"] = str(output_dir)

    # SWE-agent accepts hooks via a pre_run callback in some versions;
    # in others we patch the agent after construction.
    def _attach_hook(agent: "DefaultAgent") -> None:
        agent.add_hook(hook)

    cfg = RunSingleConfig(**config_dict)
    swe_run(cfg, agent_callback=_attach_hook)

    # Extract patch from SWE-agent output
    patch_path = output_dir / "patch.diff"
    if patch_path.exists():
        return patch_path.read_text()
    return ""


def _run_via_subprocess(
    instance: InstancePartition,
    arm: str,
    daemon_path: str,
    model: str,
    budget: int,
    config_path: str,
    output_dir: Path,
    problem_path: str,
) -> str:
    """
    Fallback: run SWE-agent as a subprocess. The hook runs via a wrapper
    script that imports wake_hook and attaches it before calling SWE-agent.
    """
    wrapper = Path(__file__).parent / "swe_agent_wrapper.py"
    env = os.environ.copy()
    env["WAKE_DAEMON_PATH"] = daemon_path
    env["WAKE_ARM"] = arm
    env["WAKE_INSTANCE_ID"] = instance.instance_id
    env["WAKE_OUTPUT_DIR"] = str(output_dir)
    env["PYTHONPATH"] = str(Path(__file__).parent) + ":" + env.get("PYTHONPATH", "")

    cmd = [
        sys.executable, str(wrapper),
        "--config", config_path,
        "--agent.model.name", model,
        "--env.repo.repo_name", instance.repo,
        "--env.repo.base_commit", instance.base_commit,
        "--problem_statement.path", problem_path,
        "--output_dir", str(output_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2000:])

    patch_path = output_dir / "patch.diff"
    return patch_path.read_text() if patch_path.exists() else ""


# ---------------------------------------------------------------------------
# Test evaluation via SWE-bench harness
# ---------------------------------------------------------------------------

def _evaluate_patch(
    instance: InstancePartition,
    patch: str,
    output_dir: Path,
) -> tuple[dict[str, bool], dict[str, bool], bool]:
    """
    Apply patch to the repo and run the test suite inside Docker.
    Returns (fail_to_pass_results, pass_to_pass_results, resolved).
    """
    if not patch:
        ftp = {t: False for t in instance.fail_to_pass}
        ptp = {t: False for t in instance.pass_to_pass}
        return ftp, ptp, False

    patch_path = output_dir / "patch.diff"
    patch_path.write_text(patch)

    # Try the Python API first; fall back to the stable CLI if unavailable.
    # Only catch ImportError here — a runtime error inside run_instance_tests
    # is a real bug and should propagate, not be silently swallowed.
    try:
        from swebench.harness.run_evaluation import run_instance_tests
        results = run_instance_tests(
            instance_id=instance.instance_id,
            patch_path=str(patch_path),
            repo=instance.repo,
            base_commit=instance.base_commit,
            fail_to_pass=instance.fail_to_pass,
            pass_to_pass=instance.pass_to_pass,
        )
        ftp_results = {t: results.get(t, False) for t in instance.fail_to_pass}
        ptp_results = {t: results.get(t, False) for t in instance.pass_to_pass}
        resolved = all(ftp_results.values()) and all(ptp_results.values())
        return ftp_results, ptp_results, resolved
    except ImportError:
        # run_instance_tests not available in this swebench version — use CLI.
        return _evaluate_via_cli(instance, str(patch_path), output_dir)


def _evaluate_via_cli(
    instance: InstancePartition,
    patch_path: str,
    output_dir: Path,
) -> tuple[dict[str, bool], dict[str, bool], bool]:
    """Run swebench evaluation via CLI subprocess."""
    predictions_path = output_dir / "predictions.jsonl"
    with open(predictions_path, "w") as f:
        json.dump({
            "instance_id": instance.instance_id,
            "model_patch": Path(patch_path).read_text(),
            "model_name_or_path": "wake-eval",
        }, f)
        f.write("\n")

    result_dir = output_dir / "eval_results"
    result_dir.mkdir(exist_ok=True)

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", "princeton-nlp/SWE-bench_Verified",
        "--predictions_path", str(predictions_path),
        "--max_workers", "1",
        "--run_id", instance.instance_id,
        "--output_dir", str(result_dir),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=1800)

    results_file = result_dir / f"{instance.instance_id}.json"
    if results_file.exists():
        data = json.loads(results_file.read_text())
        tests_status = data.get("tests_status", {})
        ftp_results = {t: tests_status.get(t) == "PASSED" for t in instance.fail_to_pass}
        ptp_results = {t: tests_status.get(t) == "PASSED" for t in instance.pass_to_pass}
        resolved = all(ftp_results.values()) and all(ptp_results.values())
        return ftp_results, ptp_results, resolved

    ftp = {t: False for t in instance.fail_to_pass}
    ptp = {t: False for t in instance.pass_to_pass}
    return ftp, ptp, False


# ---------------------------------------------------------------------------
# Per-instance entry point
# ---------------------------------------------------------------------------

def run_instance(
    instance: InstancePartition,
    daemon_path: str,
    output_dir: Path,
    model: str = "claude-sonnet-4-6",
    budget: int = 5,
    config_path: str | None = None,
    arms: list[str] | None = None,
) -> InstanceResult:
    """Run one instance through both arms and return results."""
    if config_path is None:
        config_path = str(Path(__file__).parent / "config" / "swe_agent_wake.yaml")
    if arms is None:
        arms = ["wake", "ablation"]

    output_dir = output_dir / instance.instance_id
    output_dir.mkdir(parents=True, exist_ok=True)

    arm_results: dict[str, ArmResult] = {}

    for arm in arms:
        print(f"  [{arm}] running SWE-agent...", end="", flush=True)
        patch, error = _run_swe_agent(
            instance, arm, daemon_path, output_dir, model, budget, config_path
        )

        if error:
            print(f" ERROR: {error[:80]}")
            arm_results[arm] = ArmResult(
                arm=arm, instance_id=instance.instance_id,
                patch="", resolved=False, error=error
            )
            continue

        print(" evaluating...", end="", flush=True)
        ftp_res, ptp_res, resolved = _evaluate_patch(instance, patch, output_dir / arm)

        # Check if wake fired during this arm
        findings_path = output_dir / arm / f"{instance.instance_id}_{arm}.json"
        wake_fired = False
        if findings_path.exists():
            log = json.loads(findings_path.read_text())
            wake_fired = len(log.get("findings", [])) > 0

        status = "RESOLVED" if resolved else "NOT RESOLVED"
        print(f" {status}")

        arm_results[arm] = ArmResult(
            arm=arm,
            instance_id=instance.instance_id,
            patch=patch,
            resolved=resolved,
            fail_to_pass_results=ftp_res,
            pass_to_pass_results=ptp_res,
            wake_fired=wake_fired,
            wake_findings_path=str(findings_path),
            error=error,
        )

    # Save per-instance result
    result = InstanceResult(
        instance_id=instance.instance_id,
        wake=arm_results.get("wake", ArmResult("wake", instance.instance_id, "", False)),
        ablation=arm_results.get("ablation", ArmResult("ablation", instance.instance_id, "", False)),
    )
    result_path = output_dir / "result.json"
    with open(result_path, "w") as f:
        json.dump({
            "instance_id": result.instance_id,
            "wake": _arm_to_dict(result.wake),
            "ablation": _arm_to_dict(result.ablation),
        }, f, indent=2)

    return result


def _arm_to_dict(r: ArmResult) -> dict:
    return {
        "arm": r.arm,
        "resolved": r.resolved,
        "wake_fired": r.wake_fired,
        "fail_to_pass": r.fail_to_pass_results,
        "pass_to_pass": r.pass_to_pass_results,
        "error": r.error,
    }


def _load_result(path: Path) -> InstanceResult:
    """Load a persisted InstanceResult from a result.json file."""
    d = json.loads(path.read_text())

    def _arm(ad: dict, arm: str) -> ArmResult:
        return ArmResult(
            arm=arm,
            instance_id=d["instance_id"],
            patch="",
            resolved=ad.get("resolved", False),
            fail_to_pass_results=ad.get("fail_to_pass", {}),
            pass_to_pass_results=ad.get("pass_to_pass", {}),
            wake_fired=ad.get("wake_fired", False),
            error=ad.get("error", ""),
        )

    return InstanceResult(
        instance_id=d["instance_id"],
        wake=_arm(d.get("wake", {}), "wake"),
        ablation=_arm(d.get("ablation", {}), "ablation"),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from partition import load_dataset, select_instances

    p = argparse.ArgumentParser()
    p.add_argument("--instance-id", required=True)
    p.add_argument("--dataset", default="swebench_verified")
    p.add_argument("--daemon", default="../../target/release/wake-daemon")
    p.add_argument("--output-dir", default="./results")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument("--budget", type=int, default=5)
    p.add_argument("--arms", nargs="+", default=["wake", "ablation"])
    args = p.parse_args()

    instances = load_dataset(args.dataset)
    selected = select_instances(instances, instance_ids=[args.instance_id])
    if not selected:
        print(f"Instance {args.instance_id!r} not found in dataset")
        sys.exit(1)

    result = run_instance(
        instance=selected[0],
        daemon_path=args.daemon,
        output_dir=Path(args.output_dir),
        model=args.model,
        budget=args.budget,
        arms=args.arms,
    )
    print(f"\nWake:     {'RESOLVED' if result.wake.resolved else 'NOT RESOLVED'}")
    print(f"Ablation: {'RESOLVED' if result.ablation.resolved else 'NOT RESOLVED'}")
