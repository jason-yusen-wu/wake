"""
probe/oracle/harness.py — Rung 2 Wizard-of-Oz oracle experiment.

Measures the lift ceiling: how much does perfect analysis feedback help a
model fix a bug it would otherwise miss?

Experimental design (two arms, one instance each):

  ORACLE arm
    Input:  problem statement + buggy source file + oracle finding
    The oracle finding is a wake-style message naming the exact variable,
    line, and flow that constitutes the bug.

  ABLATION arm
    Input:  problem statement + buggy source file  (no finding)

Both arms see the same code.  The ONLY difference is whether the oracle
finding is present.  Neither arm sees the gold patch — showing the patch
to the model gives it the answer directly and eliminates any possible signal.

Success criterion (two-level):
  structural  — model returned a Python code block at all
  patch_coverage — fraction of non-trivial lines from the gold patch that
                   appear in the model's output, measuring whether the model
                   made the correct change rather than any change

The structural metric is sufficient to measure delta when n is large;
patch_coverage gives quality signal per instance.

The buggy source file is fetched from GitHub at instance.base_commit and
cached locally so repeated runs are fast.  Instances where the fetch fails
are skipped and counted in a separate "skipped" bucket.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "harness" / "eval"))
from partition import InstancePartition

try:
    import anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

FEEDBACK_DIR = Path(__file__).parent / "feedback"
RESULTS_DIR  = Path(__file__).parent / "results"
REPORTS_DIR  = Path(__file__).parent / "reports"
CACHE_DIR    = Path(__file__).parent / "cache"
MANIFEST_PATH = REPORTS_DIR / "run_manifest.json"

MAX_FILE_LINES = 300   # truncate very large files to keep prompts manageable

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class OracleRunResult:
    instance_id: str
    arm: str                          # "oracle" or "ablation"
    success: bool                     # structural: model returned a code block
    iterations: int
    patch_coverage_score: float = 0.0 # fraction of gold-patch additions in output
    buggy_file_fetched: bool = False  # False = degraded run, file unavailable
    latency_ms: list[float] = field(default_factory=list)
    final_patch: str = ""
    error: str = ""


@dataclass
class OraclePairResult:
    instance_id: str
    oracle: OracleRunResult
    ablation: OracleRunResult
    filepath: str = ""   # primary file used in the experiment


# ---------------------------------------------------------------------------
# File fetching
# ---------------------------------------------------------------------------

def _fetch_url(url: str, cache_path: Path) -> str:
    """GET a URL, cache the result, return content.  Returns "" on failure."""
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8", errors="replace")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wake-oracle/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(content, encoding="utf-8")
        return content
    except Exception:
        return ""


def fetch_buggy_file(
    repo: str, base_commit: str, filepath: str, instance_id: str
) -> str:
    """
    Fetch the file at base_commit (the pre-fix, buggy version) from GitHub.
    Caches to CACHE_DIR/<instance_id>/<safe_name> to avoid re-fetching.
    Returns empty string if the fetch fails (rate limit, private repo, etc.).
    """
    safe_name = re.sub(r"[/\\]", "__", filepath)
    cache_path = CACHE_DIR / instance_id / safe_name
    url = f"https://raw.githubusercontent.com/{repo}/{base_commit}/{filepath}"
    return _fetch_url(url, cache_path)


# ---------------------------------------------------------------------------
# Patch utilities
# ---------------------------------------------------------------------------

def parse_modified_python_files(gold_patch: str) -> list[str]:
    """
    Return paths of Python files modified by the gold patch, in order.
    Ignores deleted files (--- a/... with +++ b/dev/null).
    """
    files: list[str] = []
    lines = gold_patch.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("--- a/") and not line == "--- a/dev/null":
            path = line[6:]
            if not path.endswith(".py"):
                continue
            # Check the corresponding +++ line to confirm the file is modified (not deleted).
            plus_line = lines[i + 1] if i + 1 < len(lines) else ""
            if plus_line.startswith("+++ b/") and plus_line != "+++ b/dev/null":
                if path not in files:
                    files.append(path)
    return files


_TRIVIAL_LINES = {"pass", "return", "else:", "try:", "finally:", "continue", "break"}


def _significant_diff_lines(gold_patch: str, marker: str) -> list[str]:
    """
    Extract non-trivial added (marker='+') or removed (marker='-') lines from
    a unified diff, filtered down to lines that carry semantic signal.

    Skips:
      - file headers (+++ / ---)
      - lines shorter than 8 chars (too generic)
      - comment lines
      - bare structural keywords (pass/return/else:/try:/finally:/continue/break)
    """
    header = marker * 3       # '+++' or '---'
    out: list[str] = []
    for line in gold_patch.splitlines():
        if line.startswith(marker) and not line.startswith(header):
            content = line[1:].strip()
            if (
                len(content) >= 8
                and not content.startswith("#")
                and content not in _TRIVIAL_LINES
            ):
                out.append(content)
    return out


def patch_coverage(gold_patch: str, model_output: str) -> float:
    """
    Symmetric patch-coverage score:
      additions present in output  +  deletions absent from output
      ---------------------------------------------------------
                  total non-trivial diff lines

    Adds the deletion side so a model that produces the gold's added lines
    but fails to remove the buggy lines is penalized.  Without this, an
    output that keeps the buggy line *and* adds the fix can score 100%.

    A score near 1.0 means the model essentially produced the gold change.
    """
    added = _significant_diff_lines(gold_patch, "+")
    removed = _significant_diff_lines(gold_patch, "-")
    total = len(added) + len(removed)
    if total == 0:
        return 0.0

    n_added_present  = sum(1 for line in added   if line in model_output)
    n_removed_absent = sum(1 for line in removed if line not in model_output)
    return (n_added_present + n_removed_absent) / total


def _parse_hunk_ranges(gold_patch: str) -> list[tuple[int, int]]:
    """
    Extract (start_line, end_line) ranges from gold_patch hunk headers.
    Each hunk in unified diff looks like '@@ -N,M +N,M @@' where N is the
    starting line (1-indexed) and M is the count of context+removed lines.
    """
    ranges: list[tuple[int, int]] = []
    for line in gold_patch.splitlines():
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            ranges.append((start, start + count - 1))
    return ranges


def truncate_to_relevant(content: str, gold_patch: str, max_lines: int = MAX_FILE_LINES) -> str:
    """
    If the file is longer than max_lines, show the regions around the patch
    hunks rather than truncating from the top.  All hunks are kept (with
    ellipses for omitted spans) so multi-hunk fixes aren't silently clipped.

    Algorithm:
      1. Parse hunk (start, end) ranges from the patch headers.
      2. Pad each range by a context window (~max_lines / 2*num_hunks).
      3. Merge overlapping padded ranges.
      4. Emit the resulting line slices, separated by '# ... omitted ...' markers.
      5. If the merged total exceeds max_lines, shrink the context evenly.
    """
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content

    hunks = _parse_hunk_ranges(gold_patch)
    if not hunks:
        return "\n".join(lines[:max_lines])

    # Pad each hunk with context.  Start with half max_lines distributed
    # across all hunks; shrink if we overflow.
    def _build(context: int) -> list[tuple[int, int]]:
        padded = [(max(1, s - context), min(len(lines), e + context)) for s, e in hunks]
        padded.sort()
        merged: list[tuple[int, int]] = []
        for s, e in padded:
            if merged and s <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        return merged

    context = max(5, max_lines // (2 * max(1, len(hunks))))
    merged = _build(context)
    total = sum(e - s + 1 for s, e in merged)
    while total > max_lines and context > 1:
        context = max(1, context // 2)
        merged = _build(context)
        total = sum(e - s + 1 for s, e in merged)

    pieces: list[str] = []
    prev_end = 0
    for s, e in merged:
        if s > prev_end + 1:
            omitted_from = prev_end + 1
            omitted_to = s - 1
            pieces.append(f"# ... (lines {omitted_from}–{omitted_to} omitted) ...")
        # Slice is 0-indexed; hunk lines are 1-indexed.
        pieces.append("\n".join(lines[s - 1:e]))
        prev_end = e
    if prev_end < len(lines):
        pieces.append(f"# ... (lines {prev_end + 1}–{len(lines)} omitted) ...")

    return "\n".join(pieces)


# ---------------------------------------------------------------------------
# Oracle loop
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a precise Python engineer. You will be given a task description "
    "and the contents of a Python file that contains a bug. "
    "Return ONLY the complete corrected file inside a single ```python ... ``` "
    "code block with no other text."
)


def _extract_python(text: str) -> str | None:
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else None


def _build_user_blocks(
    instance: InstancePartition,
    buggy_source: str,
    feedback_text: str | None,
) -> list[dict]:
    """
    Structure the user message as content blocks so the task statement +
    buggy source (the bulk of the prompt) can be cached and shared between
    the oracle and ablation arms.  The finding/suffix is the only arm-specific
    text and sits after the cache breakpoint.
    """
    prefix = f"Task: {instance.problem_statement}\n\n"
    if buggy_source:
        prefix += f"File to fix:\n```python\n{buggy_source}\n```\n"
    else:
        prefix += "(Source file unavailable — apply your best judgment.)\n"

    suffix = ""
    if feedback_text:
        suffix += f"\nStatic analysis finding:\n{feedback_text}\n"
    suffix += "\nReturn the complete corrected file in a ```python ... ``` block."

    return [
        {
            "type": "text",
            "text": prefix,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": suffix},
    ]


def run_oracle_arm(
    instance: InstancePartition,
    feedback_text: str | None,    # None = ablation (no finding)
    buggy_source: str,            # pre-fix file content (empty = unavailable)
    model: str,
    budget: int,
    client: "anthropic.Anthropic",
) -> OracleRunResult:
    """
    Run one arm of the oracle experiment.

    oracle arm:   buggy code + oracle finding  →  model proposes fix
    ablation arm: buggy code only              →  model proposes fix

    Neither arm receives the gold patch.
    """
    arm = "oracle" if feedback_text else "ablation"
    latencies: list[float] = []

    user_blocks = _build_user_blocks(instance, buggy_source, feedback_text)
    messages: list[dict] = [{"role": "user", "content": user_blocks}]

    for iteration in range(budget):
        t0 = time.perf_counter()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8192,
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
                buggy_file_fetched=bool(buggy_source),
                error=str(exc),
            )
        latencies.append((time.perf_counter() - t0) * 1000)

        assistant_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
        proposed_fix = _extract_python(assistant_text)

        if proposed_fix is None:
            messages.append({"role": "assistant", "content": assistant_text})
            messages.append({
                "role": "user",
                "content": (
                    "Please return the complete corrected file "
                    "in a ```python ... ``` block."
                ),
            })
            continue

        coverage = patch_coverage(instance.gold_patch or "", proposed_fix)

        return OracleRunResult(
            instance_id=instance.instance_id,
            arm=arm,
            success=True,
            iterations=iteration + 1,
            patch_coverage_score=coverage,
            buggy_file_fetched=bool(buggy_source),
            latency_ms=latencies,
            final_patch=proposed_fix,
        )

    return OracleRunResult(
        instance_id=instance.instance_id,
        arm=arm,
        success=False,
        iterations=budget,
        buggy_file_fetched=bool(buggy_source),
        latency_ms=latencies,
    )


# ---------------------------------------------------------------------------
# Per-instance runner
# ---------------------------------------------------------------------------

# Single print lock so concurrent workers don't interleave their output mid-line.
_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


# ---------------------------------------------------------------------------
# Run manifest — persistent record of run status, updated per worker.
# ---------------------------------------------------------------------------

class RunManifest:
    """
    Thread-safe disk-backed manifest of run status.  Updated as each worker
    finishes so that a killed run still leaves an accurate record on disk
    of what was attempted, what succeeded, and what skipped.

    File layout (overwritten atomically each update):
      {
        "started_at": "...",
        "model": "...",
        "workers": N,
        "scope": ["id1", "id2", ...],
        "instances": {
          "id1": {"status": "ok",   "oracle_coverage": 1.0, "ablation_coverage": 0.5, "completed_at": "..."},
          "id2": {"status": "error", "error": "...", "completed_at": "..."},
          "id3": {"status": "skip_no_source",                "completed_at": "..."},
        },
        "finished_at": null,   // set when the run finishes
        "wall_time_s": null
      }
    """

    def __init__(self, path: Path, model: str, workers: int, scope: list[str]):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {
            "started_at": _now_iso(),
            "model": model,
            "workers": workers,
            "scope": sorted(scope),
            "instances": {},
            "finished_at": None,
            "wall_time_s": None,
        }
        self._flush_unlocked()

    def update(self, instance_id: str, entry: dict) -> None:
        entry = {**entry, "completed_at": _now_iso()}
        with self._lock:
            self._data["instances"][instance_id] = entry
            self._flush_unlocked()

    def finish(self, wall_time_s: float) -> None:
        with self._lock:
            self._data["finished_at"] = _now_iso()
            self._data["wall_time_s"] = round(wall_time_s, 1)
            self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(self._data, indent=2))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_buggy_source(instance: InstancePartition) -> tuple[str, str]:
    """
    Fetch + truncate the first available Python file modified by the gold patch.
    Returns (buggy_source, filepath_used) — both empty strings on failure.
    """
    for fp in parse_modified_python_files(instance.gold_patch or ""):
        content = fetch_buggy_file(
            instance.repo, instance.base_commit, fp, instance.instance_id
        )
        if content:
            return truncate_to_relevant(content, instance.gold_patch or ""), fp
    return "", ""


def run_instance(
    instance: InstancePartition,
    feedback_text: str,
    model: str,
    budget: int,
    client: "anthropic.Anthropic",
    output_dir: Path,
    manifest: "RunManifest | None" = None,
) -> OraclePairResult | None:
    """
    Run both arms concurrently for one instance.  Returns None if the buggy
    source file cannot be fetched (instance is skipped rather than run in
    degraded mode).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    buggy_source, filepath_used = _resolve_buggy_source(instance)
    if not buggy_source:
        _log(
            f"  SKIP: could not fetch buggy source for {instance.instance_id} "
            f"(repo={instance.repo}, commit={instance.base_commit[:8] if instance.base_commit else ''})"
        )
        if manifest is not None:
            manifest.update(instance.instance_id, {
                "status": "skip_no_source",
                "repo": instance.repo,
                "base_commit": instance.base_commit[:8] if instance.base_commit else "",
            })
        return None

    # Both arms are independent network-bound calls — run them in parallel.
    # Cuts wall time per instance roughly in half.  The arms race the cache
    # write so neither benefits from the other's prefix, but retry iterations
    # within an arm still hit the cache.
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_oracle = ex.submit(
            run_oracle_arm, instance, feedback_text, buggy_source, model, budget, client
        )
        f_ablation = ex.submit(
            run_oracle_arm, instance, None, buggy_source, model, budget, client
        )
        oracle_result = f_oracle.result()
        ablation_result = f_ablation.result()

    def _status(r: OracleRunResult) -> str:
        if r.success:
            return f"OK  cov={r.patch_coverage_score:.0%}"
        return f"FAIL ({r.error[:60]})" if r.error else "FAIL"

    _log(
        f"[{instance.instance_id}]  "
        f"oracle={_status(oracle_result)}   "
        f"ablation={_status(ablation_result)}"
    )

    pair = OraclePairResult(
        instance_id=instance.instance_id,
        oracle=oracle_result,
        ablation=ablation_result,
        filepath=filepath_used,
    )
    _save_pair(pair, output_dir)
    if manifest is not None:
        err = oracle_result.error or ablation_result.error
        manifest.update(instance.instance_id, {
            "status": "error" if err else "ok",
            "oracle_success": oracle_result.success,
            "ablation_success": ablation_result.success,
            "oracle_coverage": oracle_result.patch_coverage_score,
            "ablation_coverage": ablation_result.patch_coverage_score,
            "oracle_iterations": oracle_result.iterations,
            "ablation_iterations": ablation_result.iterations,
            "error": err,
        })
    return pair


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then os.replace — survives kills mid-write."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _save_pair(pair: OraclePairResult, output_dir: Path) -> None:
    payload = {
        "instance_id": pair.instance_id,
        "filepath": pair.filepath,
        "oracle": {
            "success": pair.oracle.success,
            "iterations": pair.oracle.iterations,
            "patch_coverage_score": pair.oracle.patch_coverage_score,
            "buggy_file_fetched": pair.oracle.buggy_file_fetched,
            "latency_ms": pair.oracle.latency_ms,
            "error": pair.oracle.error,
        },
        "ablation": {
            "success": pair.ablation.success,
            "iterations": pair.ablation.iterations,
            "patch_coverage_score": pair.ablation.patch_coverage_score,
            "buggy_file_fetched": pair.ablation.buggy_file_fetched,
            "latency_ms": pair.ablation.latency_ms,
            "error": pair.ablation.error,
        },
    }
    _atomic_write(
        output_dir / f"{pair.instance_id}.json",
        json.dumps(payload, indent=2),
    )
    if pair.oracle.final_patch:
        _atomic_write(
            output_dir / f"{pair.instance_id}_oracle.py",
            pair.oracle.final_patch,
        )
    if pair.ablation.final_patch:
        _atomic_write(
            output_dir / f"{pair.instance_id}_ablation.py",
            pair.ablation.final_patch,
        )


# ---------------------------------------------------------------------------
# Local instance builder
# ---------------------------------------------------------------------------

AUDIT_DATASET = Path(__file__).parent.parent / "audit" / "corpus" / "labeled_failures.jsonl"


def _load_labeled() -> dict[str, dict]:
    """Read labeled_failures.jsonl into {instance_id: record}.  Empty if missing."""
    out: dict[str, dict] = {}
    if AUDIT_DATASET.exists():
        with open(AUDIT_DATASET) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    out[r["instance_id"]] = r
    return out


def _analyzable_ids(labeled: dict[str, dict]) -> set[str]:
    """IDs marked yes/partial in the current labeled_failures dataset."""
    return {
        iid for iid, r in labeled.items()
        if r.get("would_catch") in ("yes", "partial")
    }


def _instances_from_local(ids: list[str]) -> list[InstancePartition]:
    """
    Build InstancePartition objects from local data only — no SWE-bench download.
    Sources:
      probe/oracle/feedback/<id>.json  — gold_patch (via labeled_failures.patch)
      probe/audit/corpus/labeled_failures.jsonl — problem_statement, repo, base_commit

    Only IDs that have a feedback file are returned.  Missing labeled data
    yields a partition with empty fields; the runner will skip those at
    fetch time, but we keep them so the caller sees a SKIP reason.
    """
    local = _load_labeled()
    id_set = set(ids)
    instances: list[InstancePartition] = []
    for ff in sorted(FEEDBACK_DIR.glob("*.json")):
        fb = json.loads(ff.read_text())
        iid = fb["instance_id"]
        if iid not in id_set:
            continue
        rec = local.get(iid, {})
        instances.append(InstancePartition(
            instance_id=iid,
            repo=rec.get("repo", ""),
            base_commit=rec.get("base_commit", ""),
            problem_statement=rec.get("problem_statement", ""),
            fail_to_pass=rec.get("fail_to_pass", []),
            pass_to_pass=rec.get("pass_to_pass", []),
            gold_patch=rec.get("patch", ""),
        ))
    return instances


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Rung 2 oracle harness: measure lift ceiling with perfect feedback."
    )
    p.add_argument("--instance-id", help="run a single instance by ID")
    p.add_argument("--all", action="store_true",
                   help="run all instances with oracle feedback recorded")
    p.add_argument("--dry-run", action="store_true",
                   help="list instances and check local data without calling the model")
    p.add_argument("--model", default="claude-sonnet-4-6",
                   help="model for both arms (should match Phase 8 model)")
    p.add_argument("--budget", type=int, default=3,
                   help="max iterations per arm before giving up")
    p.add_argument("--workers", type=int, default=4,
                   help="number of instances to process in parallel "
                        "(each instance still runs its two arms concurrently)")
    p.add_argument("--resume", action="store_true",
                   help="skip instances that already have a result file")
    p.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = p.parse_args()

    if args.dry_run:
        feedback_files = sorted(FEEDBACK_DIR.glob("*.json"))
        print(f"Oracle feedback files: {len(feedback_files)}")
        instances = _instances_from_local([f.stem for f in feedback_files])
        print(f"Instances with local data: {len(instances)}")
        for inst in instances:
            fps = parse_modified_python_files(inst.gold_patch or "")
            cached = any(
                (CACHE_DIR / inst.instance_id / re.sub(r"[/\\]", "__", fp)).exists()
                for fp in fps
            )
            print(
                f"  {inst.instance_id}  "
                f"files={len(fps)}  "
                f"repo={inst.repo or '(missing)'}  "
                f"commit={inst.base_commit[:8] if inst.base_commit else '(missing)'}  "
                f"cache={'hit' if cached else 'miss'}"
            )
        return

    if not _HAS_ANTHROPIC:
        print("ERROR: anthropic package not installed.  pip install anthropic")
        sys.exit(1)
    if "ANTHROPIC_API_KEY" not in os.environ:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    client = anthropic.Anthropic()

    # Build feedback map.
    feedback_map: dict[str, str] = {}
    for ff in FEEDBACK_DIR.glob("*.json"):
        fb = json.loads(ff.read_text())
        feedback_map[fb["instance_id"]] = fb["feedback_text"]

    if args.instance_id:
        ids = [args.instance_id]
    elif args.all:
        # Restrict to feedback files whose instance is in the current
        # labeled_failures.jsonl analyzable set.  Orphan feedback files
        # (from prior audit cycles) would otherwise queue and burn worker
        # slots logging "could not fetch buggy source".
        analyzable = _analyzable_ids(_load_labeled())
        ids = sorted(set(feedback_map.keys()) & analyzable)
        n_orphan = len(feedback_map) - len(ids)
        if n_orphan:
            print(
                f"Filtered: {len(ids)} analyzable feedback files in scope, "
                f"{n_orphan} orphan feedback files ignored "
                f"(not in current labeled_failures.jsonl).\n"
            )
    else:
        p.error("Specify --instance-id <id> or --all")

    instances = _instances_from_local(ids)
    if not instances:
        print(f"No local data found for IDs: {ids}")
        print("Run collect.py + autolabel.py before oracle/harness.py.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[OraclePairResult] = []
    skipped_no_feedback = 0
    skipped_done = 0
    skipped_no_source = 0

    # Pre-filter: drop instances without feedback or (if --resume) already done.
    runnable: list[tuple[InstancePartition, str]] = []
    for inst in instances:
        fb_text = feedback_map.get(inst.instance_id)
        if not fb_text:
            print(f"[{inst.instance_id}] No oracle feedback — skipping.")
            skipped_no_feedback += 1
            continue
        if args.resume and (output_dir / f"{inst.instance_id}.json").exists():
            skipped_done += 1
            continue
        runnable.append((inst, fb_text))

    print(
        f"\nRunnable: {len(runnable)}   "
        f"already-done: {skipped_done}   no-feedback: {skipped_no_feedback}\n"
        f"Workers: {args.workers}   (each runs both arms in parallel)\n"
    )

    if not runnable:
        print("Nothing to do.")
        print("Next: python eval.py")
        return

    manifest = RunManifest(
        path=MANIFEST_PATH,
        model=args.model,
        workers=args.workers,
        scope=[ifb[0].instance_id for ifb in runnable],
    )

    def _work(inst_fb: tuple[InstancePartition, str]) -> OraclePairResult | None:
        inst, fb_text = inst_fb
        return run_instance(
            inst, fb_text, args.model, args.budget, client, output_dir, manifest
        )

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_work, ifb): ifb[0].instance_id for ifb in runnable}
        for fut in as_completed(futures):
            iid = futures[fut]
            try:
                pair = fut.result()
            except Exception as exc:
                print(f"[{iid}] worker exception: {exc}")
                manifest.update(iid, {"status": "error", "error": str(exc)[:200]})
                continue
            if pair is None:
                skipped_no_source += 1
            else:
                results.append(pair)
    elapsed = time.perf_counter() - t0
    manifest.finish(elapsed)

    print(
        f"\n{len(results)} instances run,  "
        f"{skipped_no_source} skipped (no source),  "
        f"{skipped_no_feedback} skipped (no feedback),  "
        f"{skipped_done} skipped (resume).\n"
        f"Wall time: {elapsed:.0f}s   Output: {output_dir}\n"
        f"Manifest: {MANIFEST_PATH}"
    )
    print("Next: python eval.py")


if __name__ == "__main__":
    main()
