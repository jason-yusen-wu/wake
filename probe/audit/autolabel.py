"""
probe/audit/autolabel.py — Claude-powered batch labeling for the Rung 1 audit.

Calls Claude to apply the failure taxonomy to each unlabeled record in the
dataset.  Prompt caching is used so the large taxonomy/instruction block is
only charged once per session (~1.25× first call, ~0.1× every subsequent
call).  A typical 100-record run costs ~$0.10–$0.30 with claude-sonnet-4-6
or ~$0.30–$0.80 with claude-opus-4-7 depending on patch sizes.

Labeling is non-destructive: already-labeled records are skipped by default
(use --overwrite to re-label).  Each label is written to disk immediately so
a partial run can be resumed.  Records labeled by this tool set
`labeled_by = <model-id>` so they are distinguishable from human labels.

Usage:
  # Label everything unlabeled (default model: claude-opus-4-7)
  python autolabel.py

  # Cost-optimised run on a large dataset
  python autolabel.py --model claude-sonnet-4-6

  # Preview what would be labeled without spending API budget
  python autolabel.py --dry-run

  # Label at most 20 records then stop
  python autolabel.py --n 20

  # Print records the model was uncertain about (for human review)
  python autolabel.py --review-uncertain

After running, inspect the labels with:
  python analyze.py

Then correct any low-confidence labels with:
  python label.py --start 0    # interactive, will skip already-labeled
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("ERROR: anthropic package not installed.  pip install anthropic")
    sys.exit(1)

from schema import (
    AnalysisProperty,
    AnalysisVerdict,
    FailureCategory,
    LabeledFailure,
)
import dataset as ds


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-4-7"
MAX_PATCH_LINES = 60     # truncate long patches to control token cost
MAX_PS_LINES = 8         # problem statement lines shown to the model

# JSON schema for structured output — exactly matches the fields we populate.
_LABEL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": FailureCategory.choices(),
            "description": "Failure taxonomy category.",
        },
        "would_catch": {
            "type": "string",
            "enum": AnalysisVerdict.choices(),
            "description": "Would precise static analysis have caught this?",
        },
        "which_property": {
            "type": "string",
            "enum": AnalysisProperty.choices(),
            "description": "Which analysis property would catch it (or none).",
        },
        "analysis_note": {
            "type": "string",
            "description": "1–3 sentence explanation of the label.",
        },
        "labeler_confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "How confident is the labeler in this label?",
        },
    },
    "required": [
        "category",
        "would_catch",
        "which_property",
        "analysis_note",
        "labeler_confidence",
    ],
    "additionalProperties": False,
}

# ---------------------------------------------------------------------------
# Cached system prompt (stable across all calls → caches after the first)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are an expert program-analysis researcher labeling SWE-bench failure cases
for a Rung 1 failure audit.  For each case you will receive:
  • The problem statement (what the issue asks for)
  • The patch (the fix that was applied, gold or agent)
  • The source of the patch (gold / agent_failed / agent_partial / agent_regressed)
  • Any test failures / regressions

Your job is to answer two questions:

QUESTION 1 — CATEGORY
Assign exactly one failure category to this case:

  null_type          — The bug is a None-dereference, wrong-type usage, or
                       unhandled Optional.  This is what wake's nullability
                       analysis (wake-prop-null) catches today.

  incomplete_edit    — The patch changed a function or variable but missed
                       downstream callers, dependents, or related call-sites
                       that needed the same change.  This is what wake's
                       blast-radius / change-consistency analysis catches.

  wrong_logic        — The code is locally plausible but globally incorrect.
                       No type or None issue; purely a semantic logic error.

  missing_edge       — A missing branch, off-by-one, boundary condition, or
                       unhandled edge case in control flow.

  integration        — Interaction bug: serialization/deserialization, ORM
                       semantics, cross-component protocol mismatch.

  api_misuse         — Calling an API with wrong arguments, wrong order, or
                       misunderstood semantics.

  misunderstood_intent — The patch fixes something, but not what the test
                       suite requires.  Analysis cannot help here.

  other              — Doesn't fit any of the above buckets.

QUESTION 2 — WOULD STATIC ANALYSIS CATCH THIS?
Answer whether a *precise, precision-first* static analysis would have caught
this failure:

  yes      — The failure is directly within wake's current scope: a
             nullability flow or a blast-radius / missed-caller issue.
             Analysis would have fired on the correct site.

  partial  — Analysis might flag something related (e.g. a downstream
             symptom) but not the precise root cause.

  no       — Definitively outside what nullability + change-consistency
             analysis can catch (logic error, intent mismatch, etc.).

  unknown  — Not enough information in the provided context to decide.

If you answered yes or partial, also identify WHICH PROPERTY:

  nullability         — wake-prop-null: None-dereference, Optional flow
  change_consistency  — blast-radius / missed callers after an edit
  type_safety         — static type mismatch beyond nullable/non-nullable
  resource_lifetime   — use-after-free, double-close, etc.
  other               — a different analysis property
  none                — analysis would not catch it (only when verdict is no)

PRECISION REQUIREMENT
wake is precision-first: it only flags what it can confirm, never a noisy
false positive.  When deciding "yes", ask: would wake have had *enough
evidence* to flag this specific site, or would it have stayed Unknown?

OUTPUT FORMAT
Respond with a JSON object matching the schema.  Keep analysis_note to 1–3
sentences explaining your reasoning.  Set labeler_confidence to:
  high   — you are confident in both the category and the verdict
  medium — one of the two judgements is uncertain
  low    — the patch or context is too ambiguous to label reliably
""".strip()


# ---------------------------------------------------------------------------
# Per-record user prompt
# ---------------------------------------------------------------------------

def _build_user_prompt(f: LabeledFailure) -> str:
    ps_lines = f.problem_statement.strip().splitlines()[:MAX_PS_LINES]
    ps_text = "\n".join(ps_lines)
    if len(f.problem_statement.splitlines()) > MAX_PS_LINES:
        ps_text += "\n… (truncated)"

    patch_lines = f.patch.splitlines()[:MAX_PATCH_LINES]
    patch_text = "\n".join(patch_lines)
    if len(f.patch.splitlines()) > MAX_PATCH_LINES:
        patch_text += "\n… (patch truncated)"

    ftp_fail = f.failed_held_out_tests[:5]
    broke = f.broke_passing_tests[:5]
    test_info = ""
    if ftp_fail:
        test_info += f"Held-out test failures: {', '.join(ftp_fail)}\n"
    if broke:
        test_info += f"Regressions (pass→fail): {', '.join(broke)}\n"
    if not test_info:
        test_info = "(Gold-patch mode — no test run; labeling bug type from the patch.)\n"

    return textwrap.dedent(f"""
        Instance: {f.instance_id}
        Repo:     {f.repo}
        Source:   {f.patch_source.value}

        PROBLEM STATEMENT:
        {ps_text}

        PATCH:
        ```diff
        {patch_text}
        ```

        TEST INFORMATION:
        {test_info.strip()}

        Label this failure according to the taxonomy.
    """).strip()


# ---------------------------------------------------------------------------
# Single-record labeling
# ---------------------------------------------------------------------------

def label_one(
    f: LabeledFailure,
    client: anthropic.Anthropic,
    model: str,
) -> tuple[dict, object]:
    """
    Call Claude to label one failure.

    Returns ``(result_dict, response.usage)`` so the caller can track
    prompt-cache hit/write counts and verify that caching is actually working.
    Raises ``anthropic.APIError`` on API failure.
    """
    response = client.messages.create(
        model=model,
        max_tokens=1024,  # analysis_note can be verbose; 512 was tight
        # Cache the large system prompt across all records in this session.
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": _LABEL_SCHEMA,
            }
        },
        messages=[{"role": "user", "content": _build_user_prompt(f)}],
    )
    # output_config guarantees the first text block is valid JSON.
    raw = next(b.text for b in response.content if b.type == "text")
    return json.loads(raw), response.usage


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

_PRINT_LOCK = threading.Lock()
_DATASET_LOCK = threading.Lock()


def _safe_print(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, flush=True)


def _apply_label(f: LabeledFailure, result: dict, model: str) -> str:
    """Mutate f in place from a successful label result.  Returns confidence."""
    f.category = FailureCategory.from_str(result["category"])
    f.would_catch = AnalysisVerdict.from_str(result["would_catch"])
    f.which_property = AnalysisProperty.from_str(result["which_property"])
    # Post-validation: a yes/partial verdict must name a property.
    if (
        f.would_catch in (AnalysisVerdict.YES, AnalysisVerdict.PARTIAL)
        and f.which_property == AnalysisProperty.NONE
    ):
        f.which_property = AnalysisProperty.OTHER
        result["analysis_note"] = (
            "[auto: which_property set to 'other' — model returned 'none' "
            "for a yes/partial verdict] " + result.get("analysis_note", "")
        )
    f.analysis_note = result.get("analysis_note", "")
    conf = result.get("labeler_confidence", "unknown")
    if conf != "high":
        f.analysis_note = f"[confidence:{conf}] {f.analysis_note}"
    f.labeled_by = model
    f.label_timestamp = datetime.datetime.utcnow().isoformat()
    return conf


def run_autolabel(
    dataset_path: Path,
    model: str,
    n: int | None,
    overwrite: bool,
    dry_run: bool,
    workers: int = 8,
) -> None:
    failures = ds.load(dataset_path)
    if overwrite:
        to_label = failures
    else:
        to_label = [f for f in failures if not f.is_labeled]

    if n is not None:
        to_label = to_label[:n]

    if not to_label:
        if not failures:
            print("Dataset is empty — no records found.")
            print(f"  Run collect.py first:  python collect.py --source gold --n 100")
        else:
            print("Nothing to label.  All records are already labeled.")
            print("Use --overwrite to re-label, or run collect.py to add more records.")
        return

    print(f"Model:          {model}")
    print(f"Dataset:        {dataset_path}")
    print(f"Records to label: {len(to_label)}")
    print(f"Workers:        {workers}")
    if dry_run:
        print("DRY RUN — no API calls will be made.\n")
        for i, f in enumerate(to_label, 1):
            print(f"  [{i}/{len(to_label)}] {f.instance_id}  (source: {f.patch_source.value})")
        return

    client = anthropic.Anthropic()

    # Cache warmup: one synchronous call before launching the pool so the
    # large system-prompt cache block is written exactly once.  Without this,
    # workers > 1 would race to write the cache and most parallel calls would
    # miss it.  We use the first record as the warmup target.
    warmup_iid = to_label[0].instance_id
    print(f"\nCache warmup on {warmup_iid} ... ", end="", flush=True)
    cache_read_tokens = 0
    cache_write_tokens = 0
    labeled = 0
    errors = 0

    try:
        result, usage = label_one(to_label[0], client, model)
        try:
            conf = _apply_label(to_label[0], result, model)
            with _DATASET_LOCK:
                ds.upsert(to_label[0], dataset_path)
            labeled += 1
            cache_read_tokens  += getattr(usage, "cache_read_input_tokens",  0) or 0
            cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            print(f"OK ({to_label[0].category.value} / {to_label[0].would_catch.value} [{conf}])")
        except (ValueError, KeyError) as exc:
            errors += 1
            print(f"SCHEMA ERROR: {exc}")
    except (anthropic.APIError, json.JSONDecodeError, StopIteration) as exc:
        errors += 1
        print(f"API/PARSE ERROR (cache not primed): {exc}")

    remaining = to_label[1:]
    if not remaining:
        # Single-record run; warmup was the whole job.
        pass
    else:
        print(f"Launching {workers} workers on {len(remaining)} remaining records...\n")
        progress = {"done": 0, "total": len(remaining)}

        def _label_one_worker(f: LabeledFailure) -> tuple[str, str, int, int] | tuple[str, str]:
            """Return ('ok', short_status, cache_read, cache_write) or ('err', msg)."""
            try:
                result, usage = label_one(f, client, model)
            except anthropic.APIError as exc:
                return ("err", f"API ERROR: {exc}")
            except (json.JSONDecodeError, StopIteration) as exc:
                return ("err", f"PARSE ERROR: {exc}")
            try:
                conf = _apply_label(f, result, model)
            except (ValueError, KeyError) as exc:
                return ("err", f"SCHEMA ERROR: {exc}")
            with _DATASET_LOCK:
                ds.upsert(f, dataset_path)
            r = getattr(usage, "cache_read_input_tokens", 0) or 0
            w = getattr(usage, "cache_creation_input_tokens", 0) or 0
            return ("ok", f"{f.category.value} / {f.would_catch.value} [{conf}]", r, w)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(_label_one_worker, f): f for f in remaining}
            for fut in as_completed(futures):
                f = futures[fut]
                progress["done"] += 1
                i = progress["done"]
                total = progress["total"]
                try:
                    res = fut.result()
                except Exception as exc:
                    errors += 1
                    _safe_print(f"  [{i}/{total}] {f.instance_id} WORKER EXC: {exc}")
                    continue
                if res[0] == "err":
                    errors += 1
                    _safe_print(f"  [{i}/{total}] {f.instance_id} {res[1]}")
                else:
                    _, status, r, w = res
                    cache_read_tokens  += r
                    cache_write_tokens += w
                    labeled += 1
                    _safe_print(f"  [{i}/{total}] {f.instance_id} {status}")

    total_cache = cache_read_tokens + cache_write_tokens
    cache_pct = f"{cache_read_tokens / total_cache:.0%}" if total_cache else "N/A"
    print()
    print(f"Done.  Labeled: {labeled}   Errors: {errors}")
    print(f"Cache: {cache_read_tokens:,} read tokens / {cache_write_tokens:,} write tokens "
          f"({cache_pct} served from cache)")
    if labeled > 1 and cache_read_tokens == 0:
        print("WARNING: cache_read_tokens is 0 — prompt caching may not be working. "
              "Check ANTHROPIC_API_KEY tier and minimum cacheable prefix size.")

    # ── Write reports for review ──────────────────────────────────────────────
    # Use a fixed reports dir relative to this script, regardless of --dataset.
    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Reload the full dataset so the TSV reflects ALL labeled records.
    all_records = ds.load(dataset_path)
    all_labeled = [r for r in all_records if r.is_labeled]

    # Track which instance IDs were labeled THIS session so the JSON log is
    # scoped to the current run, not the entire history.
    session_ids = {f.instance_id for f in to_label[:labeled + errors]}
    session_records = [r for r in all_labeled if r.instance_id in session_ids and r.is_labeled]

    # 1. TSV summary: one row per record in the full labeled dataset.
    tsv_path = reports_dir / "autolabel_summary.tsv"
    with open(tsv_path, "w") as tsv:
        tsv.write("instance_id\tcategory\twould_catch\twhich_property\tconfidence\tnote\n")
        for r in all_labeled:
            conf = "high"
            note = r.analysis_note
            if note.startswith("[confidence:"):
                end = note.index("]")
                conf = note[len("[confidence:"):end]
                note = note[end + 2:]
            elif note.startswith("[auto:"):
                conf = "auto-corrected"
            tsv.write(
                f"{r.instance_id}\t{r.category.value}\t{r.would_catch.value}\t"
                f"{r.which_property.value}\t{conf}\t{note[:120]}\n"
            )

    # 2. JSON session log: this run only (not the full history).
    # (datetime is imported at the top of the module)
    log_path = reports_dir / "autolabel_log.json"
    with open(log_path, "w") as jf:
        json.dump({
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "model": model,
            "session_labeled": labeled,
            "session_errors": errors,
            "total_labeled_in_dataset": len(all_labeled),
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "session_records": [
                {
                    "instance_id": r.instance_id,
                    "repo": r.repo,
                    "category": r.category.value,
                    "would_catch": r.would_catch.value,
                    "which_property": r.which_property.value,
                    "analysis_note": r.analysis_note,
                    "labeled_by": r.labeled_by,
                }
                for r in session_records
            ],
        }, jf, indent=2)

    print(f"\nReports written:")
    print(f"  TSV summary  → {tsv_path}")
    print(f"  JSON log     → {log_path}")
    print(f"\nNext: python analyze.py --dataset {dataset_path}")


# ---------------------------------------------------------------------------
# Review uncertain labels
# ---------------------------------------------------------------------------

def review_uncertain(dataset_path: Path) -> None:
    """Print records labeled by the model with low/medium confidence."""
    failures = ds.load(dataset_path)
    uncertain = [
        f for f in failures
        if f.is_labeled
        and f.labeled_by != "human"
        and "[confidence:" in f.analysis_note
    ]
    if not uncertain:
        print("No uncertain AI-labeled records found.")
        print("Either run autolabel.py first, or all labels are high-confidence.")
        return

    print(f"\nUncertain AI labels ({len(uncertain)} records) — review with label.py:\n")
    print(f"  {'Instance':<40} {'Category':<22} {'Verdict':<10} {'Confidence'}")
    print("  " + "-" * 85)
    for f in uncertain:
        # Extract confidence tag from the note prefix.
        conf = "medium"
        if "[confidence:low]" in f.analysis_note:
            conf = "low"
        print(f"  {f.instance_id:<40} {f.category.value:<22} {f.would_catch.value:<10} {conf}")
    print()
    print("To correct these labels interactively, run:")
    print("  python label.py")
    print("(label.py will prompt for the uncertain cases in order;")
    print(" already-labeled records are skipped unless you change --start)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Use Claude to apply the Rung 1 taxonomy to unlabeled failures.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Cost guidance:
              claude-opus-4-7  — best quality, ~$0.03–$0.08 per record
              claude-sonnet-4-6 — good quality, ~$0.01–$0.02 per record
            Prompt caching (~90%% savings) kicks in after the first record.
        """),
    )
    p.add_argument("--dataset", default=str(ds.DEFAULT_DATASET),
                   help="path to labeled_failures.jsonl")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   choices=["claude-opus-4-7", "claude-sonnet-4-6",
                            "claude-haiku-4-5"],
                   help=f"model to use for labeling (default: {DEFAULT_MODEL})")
    p.add_argument("--n", type=int, default=None,
                   help="label at most N records (useful for pilots)")
    p.add_argument("--workers", type=int, default=8,
                   help="parallel API workers (default: 8); a cache warmup "
                        "runs synchronously before the pool launches")
    p.add_argument("--smoke-test", action="store_true",
                   help="label exactly 1 record to verify API connectivity, "
                        "then stop (equivalent to --n 1 --overwrite)")
    p.add_argument("--overwrite", action="store_true",
                   help="re-label records that already have labels")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be labeled without calling the API")
    p.add_argument("--review-uncertain", action="store_true",
                   help="show AI-labeled records with low/medium confidence")
    args = p.parse_args()

    if "ANTHROPIC_API_KEY" not in __import__("os").environ and not args.dry_run and not args.review_uncertain:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    path = Path(args.dataset)

    if args.review_uncertain:
        review_uncertain(path)
        return

    if args.smoke_test:
        print("Smoke-test mode: labeling 1 record to verify API connectivity.")
        args.n = 1
        args.overwrite = True

    run_autolabel(
        dataset_path=path,
        model=args.model,
        n=args.n,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
