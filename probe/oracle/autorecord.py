"""
probe/oracle/autorecord.py — generate oracle feedback automatically using Claude.

Replaces the interactive record.py for automated pipeline runs.  For each
analyzable instance in the labeled dataset that does not yet have oracle
feedback, asks Claude to write the message a *perfect* wake-style static
analyzer would produce before the fix is applied.

The generated feedback is saved to probe/oracle/feedback/<instance_id>.json
with recorded_by = <model-id> so it is distinguishable from human-written
feedback.

Quality expectation: the feedback should name the specific variable, function,
and approximate line from the gold patch, describe the value-flow path, and
use wake's output format so the oracle harness model finds it actionable.

Usage:
  python autorecord.py
  python autorecord.py --model claude-sonnet-4-6   # cheaper
  python autorecord.py --n 20                       # first 20 only
  python autorecord.py --overwrite                  # re-generate all
  python autorecord.py --dry-run                    # show pending, no API calls
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed.  pip install anthropic")
    sys.exit(1)

FEEDBACK_DIR = Path(__file__).parent / "feedback"
AUDIT_DEFAULT = Path(__file__).parent.parent / "audit" / "corpus" / "labeled_failures.jsonl"
DEFAULT_MODEL = "claude-sonnet-4-6"   # sonnet is sufficient; oracle feedback is shorter than labels

MAX_PATCH_LINES = 80
MAX_PS_LINES = 10

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_SYSTEM = """
You are a precise static-analysis tool that has already found a bug in a
Python codebase.  Your job is to produce the finding message you would give
to a coding agent — BEFORE the agent has made any fix.

Format rules (strictly):
  [HIGH|MEDIUM|LOW] Root cause: <one-line description of the bug source>
    • line <N>: '<variable>' used as <attribute|subscript|call> — <brief trace from source to consumer>
    • (additional consumer lines if more than one)
    Suggested fix location: line <N> (<brief description>)

Additional rules:
  - Be specific: name the exact variable, function, and line number from the patch.
  - Describe the PROBLEM, not the fix.
  - 3–6 lines total.  Under 120 tokens.
  - If the bug is a missed-caller / incomplete-edit, describe which callers were
    not updated rather than a None-dereference.
  - Output ONLY the finding message — no preamble, no explanation, no code.
""".strip()

_PROPERTY_HINT = {
    "nullability": (
        "The bug is a None-dereference or unhandled Optional.  "
        "Describe which variable can be None, where it flows from, and where it is consumed."
    ),
    "change_consistency": (
        "The bug is an incomplete edit: a function or variable was changed but downstream "
        "callers or dependents were not updated.  Describe which callers were missed."
    ),
    "type_safety": (
        "The bug is a static type mismatch.  Describe the type error and where it occurs."
    ),
    "other": (
        "Describe the static-analysis finding that would catch this bug precisely."
    ),
}


def _build_prompt(record: dict) -> str:
    ps = "\n".join(record.get("problem_statement", "").strip().splitlines()[:MAX_PS_LINES])
    patch = "\n".join(record.get("patch", "").splitlines()[:MAX_PATCH_LINES])
    prop = record.get("which_property", "other")
    hint = _PROPERTY_HINT.get(prop, _PROPERTY_HINT["other"])
    note = record.get("analysis_note", "")

    return (
        f"Instance: {record['instance_id']}\n"
        f"Repo:     {record.get('repo', '')}\n\n"
        f"PROBLEM STATEMENT:\n{ps}\n\n"
        f"GOLD PATCH (the correct fix — use this to infer the bug):\n"
        f"```diff\n{patch}\n```\n\n"
        f"ANALYSIS LABEL:\n"
        f"  category      = {record.get('category', '')}\n"
        f"  which_property = {prop}\n"
        f"  analysis_note  = {note}\n\n"
        f"GUIDANCE: {hint}\n\n"
        f"Write the static-analysis finding message."
    )


# ---------------------------------------------------------------------------
# Single-record generation
# ---------------------------------------------------------------------------

def generate_one(record: dict, client: anthropic.Anthropic, model: str) -> tuple[str, object]:
    """
    Generate oracle feedback for one record.
    Returns (feedback_text, response.usage).
    Raises anthropic.APIError on failure.
    """
    response = client.messages.create(
        model=model,
        max_tokens=256,
        # System prompt is stable across all records — cache it.
        system=[{
            "type": "text",
            "text": _SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": _build_prompt(record)}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return text.strip(), response.usage


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

_PRINT_LOCK = threading.Lock()


def _safe_print(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


_CONFIDENCE = {
    "nullability": "high",
    "change_consistency": "medium",
}


def _write_feedback(record: dict, feedback_text: str, model: str) -> None:
    iid = record["instance_id"]
    prop = record.get("which_property", "?")
    confidence = _CONFIDENCE.get(prop, "low")
    fb = {
        "instance_id": iid,
        "feedback_text": feedback_text,
        "category": record.get("category", ""),
        "which_property": prop,
        "confidence": confidence,
        "is_analyzable": True,
        "gold_patch": record.get("patch", ""),
        "recorded_by": model,
        "record_timestamp": datetime.datetime.utcnow().isoformat(),
    }
    out_path = FEEDBACK_DIR / f"{iid}.json"
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fb, indent=2))
    import os
    os.replace(tmp, out_path)


def run_autorecord(
    audit_path: Path,
    model: str,
    n: int | None,
    overwrite: bool,
    dry_run: bool,
    workers: int = 8,
) -> None:
    if not audit_path.exists():
        print(f"Audit dataset not found: {audit_path}")
        print("Run probe/audit/collect.py and probe/audit/autolabel.py first.")
        return

    with open(audit_path) as f:
        all_records = [json.loads(line) for line in f if line.strip()]

    analyzable = [r for r in all_records if r.get("would_catch") in ("yes", "partial")]
    if not analyzable:
        print("No analyzable failures found in the dataset.")
        print("Run autolabel.py (or label.py) first so records have would_catch labels.")
        return

    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    if overwrite:
        pending = analyzable
    else:
        pending = [r for r in analyzable
                   if not (FEEDBACK_DIR / f"{r['instance_id']}.json").exists()]

    if n is not None:
        pending = pending[:n]

    print(f"Analyzable: {len(analyzable)}   Pending (no feedback yet): {len(pending)}")
    print(f"Workers:    {workers}")

    if not pending:
        print("All analyzable instances already have oracle feedback.")
        print("Use --overwrite to regenerate.")
        return

    if dry_run:
        print("DRY RUN — no API calls.\n")
        for r in pending:
            print(f"  {r['instance_id']}  ({r.get('which_property', '?')})")
        return

    client = anthropic.Anthropic()
    generated = 0
    errors = 0
    cache_read = 0
    cache_write = 0

    # Cache warmup: one synchronous call so the cached system prompt is
    # written once before the parallel pool starts.
    warmup = pending[0]
    print(f"\nCache warmup on {warmup['instance_id']} ... ", end="", flush=True)
    try:
        feedback_text, usage = generate_one(warmup, client, model)
        _write_feedback(warmup, feedback_text, model)
        cache_read  += getattr(usage, "cache_read_input_tokens",  0) or 0
        cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        generated += 1
        print("OK")
    except (anthropic.APIError, StopIteration) as exc:
        errors += 1
        print(f"ERROR (cache not primed): {exc}")

    remaining = pending[1:]
    if remaining:
        print(f"Launching {workers} workers on {len(remaining)} remaining records...\n")

        def _gen_worker(record: dict) -> tuple[str, str, int, int] | tuple[str, str]:
            iid = record["instance_id"]
            try:
                feedback_text, usage = generate_one(record, client, model)
            except anthropic.APIError as exc:
                return ("err", f"API ERROR: {exc}")
            except StopIteration:
                return ("err", "PARSE ERROR: no text block in response")
            _write_feedback(record, feedback_text, model)
            r = getattr(usage, "cache_read_input_tokens", 0) or 0
            w = getattr(usage, "cache_creation_input_tokens", 0) or 0
            return ("ok", iid, r, w)

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_gen_worker, r): r for r in remaining}
            for fut in as_completed(futures):
                rec = futures[fut]
                done += 1
                try:
                    res = fut.result()
                except Exception as exc:
                    errors += 1
                    _safe_print(f"  [{done}/{len(remaining)}] {rec['instance_id']} WORKER EXC: {exc}")
                    continue
                if res[0] == "err":
                    errors += 1
                    _safe_print(f"  [{done}/{len(remaining)}] {rec['instance_id']} ({rec.get('which_property','?')}) {res[1]}")
                else:
                    _, iid, r, w = res
                    cache_read  += r
                    cache_write += w
                    generated += 1
                    _safe_print(f"  [{done}/{len(remaining)}] {iid} ({rec.get('which_property','?')}) OK")

    total_cache = cache_read + cache_write
    cache_pct = f"{cache_read / total_cache:.0%}" if total_cache else "N/A"
    print()
    print(f"Done.  Generated: {generated}   Errors: {errors}")
    print(f"Cache: {cache_read:,} read / {cache_write:,} write ({cache_pct} from cache)")
    print(f"Feedback saved to: {FEEDBACK_DIR}/")
    print(f"\nNext: python harness.py --all")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Auto-generate oracle feedback using Claude (no human input required)."
    )
    p.add_argument("--audit-dataset", default=str(AUDIT_DEFAULT),
                   help="path to labeled_failures.jsonl")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   choices=["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
                   help=f"model for feedback generation (default: {DEFAULT_MODEL})")
    p.add_argument("--n", type=int, default=None,
                   help="generate for at most N instances")
    p.add_argument("--workers", type=int, default=8,
                   help="parallel API workers (default: 8); a cache warmup "
                        "runs synchronously before the pool launches")
    p.add_argument("--overwrite", action="store_true",
                   help="regenerate even for instances that already have feedback")
    p.add_argument("--dry-run", action="store_true",
                   help="list pending instances without calling the API")
    args = p.parse_args()

    import os
    if "ANTHROPIC_API_KEY" not in os.environ and not args.dry_run:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    run_autorecord(
        audit_path=Path(args.audit_dataset),
        model=args.model,
        n=args.n,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
