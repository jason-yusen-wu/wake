"""
probe/audit/label.py — interactive terminal tool for labeling failures.

For each unlabeled record it shows:
  • instance ID, repo, source
  • problem statement (truncated)
  • the patch (truncated to first 80 lines)
  • which tests failed

Then prompts for:
  1. Category (one of the taxonomy values)
  2. Would analysis catch it? (yes / partial / no / unknown)
  3. Which property? (only if yes/partial)
  4. Free-text note (optional)

Saves each label immediately so a partial session is not lost.
Press Ctrl-C to pause; run again to resume from the first unlabeled record.

Usage:
  python label.py
  python label.py --dataset corpus/labeled_failures.jsonl --start 10
"""
from __future__ import annotations

import argparse
import datetime
import sys
import textwrap
from pathlib import Path

from schema import (
    AnalysisProperty, AnalysisVerdict, FailureCategory, LabeledFailure,
)
import dataset as ds

# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"


def _h(s: str) -> str:
    return f"{_BOLD}{_CYAN}{s}{_RESET}"


def _dim(s: str) -> str:
    return f"{_DIM}{s}{_RESET}"


def _prompt(label: str, choices: list[str], default: str | None = None) -> str:
    choice_str = "/".join(choices)
    if default:
        choice_str = "/".join(f"[{c}]" if c == default else c for c in choices)
    while True:
        raw = input(f"  {_BOLD}{label}{_RESET} ({choice_str}): ").strip().lower()
        if not raw and default:
            return default
        if raw in choices:
            return raw
        print(f"  {_RED}Invalid — choose from: {', '.join(choices)}{_RESET}")


def _prompt_text(label: str) -> str:
    raw = input(f"  {_BOLD}{label}{_RESET} (optional, Enter to skip): ").strip()
    return raw


def _divider(char: str = "─", width: int = 72) -> None:
    print(_dim(char * width))


def _show_failure(f: LabeledFailure, index: int, total: int) -> None:
    print()
    _divider("═")
    print(_h(f"[{index + 1}/{total}] {f.instance_id}"))
    print(f"  Repo:   {f.repo}   Source: {f.patch_source.value}")
    _divider()

    # Problem statement (first 5 lines)
    ps_lines = f.problem_statement.strip().splitlines()[:5]
    print(_YELLOW + "Problem:" + _RESET)
    for line in ps_lines:
        print("  " + textwrap.shorten(line, width=76))
    if len(f.problem_statement.splitlines()) > 5:
        print(_dim("  … (truncated)"))

    _divider()

    # Patch (first 80 lines)
    patch_lines = f.patch.splitlines()[:80]
    if patch_lines:
        print(_YELLOW + "Patch:" + _RESET)
        for line in patch_lines:
            colour = _GREEN if line.startswith("+") else (_RED if line.startswith("-") else _dim(""))
            reset = _RESET if colour else ""
            print(f"  {colour}{line[:100]}{reset}")
        if len(f.patch.splitlines()) > 80:
            print(_dim("  … (patch truncated)"))
    else:
        print(_dim("  (no patch)"))

    _divider()

    # Test outcome
    ftp_fail = f.failed_held_out_tests
    broke = f.broke_passing_tests
    if ftp_fail:
        print(f"{_RED}Held-out tests failed ({len(ftp_fail)}):{_RESET}")
        for t in ftp_fail[:5]:
            print(f"  {t}")
        if len(ftp_fail) > 5:
            print(_dim(f"  … and {len(ftp_fail) - 5} more"))
    if broke:
        print(f"{_RED}Regressions — previously-passing tests now fail ({len(broke)}):{_RESET}")
        for t in broke[:5]:
            print(f"  {t}")
    if not ftp_fail and not broke:
        print(_dim("  (gold mode — no test run; labeling the bug type itself)"))
    print()


# ---------------------------------------------------------------------------
# Core labeling loop
# ---------------------------------------------------------------------------

def label_one(f: LabeledFailure) -> LabeledFailure:
    """Prompt for all labels on a single failure. Returns updated record."""
    cat_str = _prompt(
        "Category",
        FailureCategory.choices(),
    )
    f.category = FailureCategory.from_str(cat_str)

    verdict_str = _prompt(
        "Would analysis catch this?",
        AnalysisVerdict.choices(),
        default="unknown",
    )
    f.would_catch = AnalysisVerdict.from_str(verdict_str)

    if f.would_catch in (AnalysisVerdict.YES, AnalysisVerdict.PARTIAL):
        prop_str = _prompt(
            "Which property?",
            AnalysisProperty.choices(),
            default="none",
        )
        f.which_property = AnalysisProperty.from_str(prop_str)
    else:
        f.which_property = AnalysisProperty.NONE

    note = _prompt_text("Note (why/how analysis would or wouldn't catch this)")
    f.analysis_note = note
    f.labeled_by = "human"
    f.label_timestamp = datetime.datetime.utcnow().isoformat()
    return f


def run_labeling(
    dataset_path: Path,
    start: int = 0,
) -> None:
    failures = ds.load(dataset_path)
    unlabeled = [f for f in failures if not f.is_labeled]

    if not unlabeled:
        print("All records are already labeled. Nothing to do.")
        return

    to_label = unlabeled[start:]
    print(f"\n{_h('Wake Rung 1 — Failure Audit Labeling Tool')}")
    print(f"  Dataset:   {dataset_path}")
    print(f"  Unlabeled: {len(unlabeled)}   Starting at: {start + 1}")
    print("  Press Ctrl-C to pause. Run again to resume.")
    print()

    labeled_count = 0
    try:
        for i, f in enumerate(to_label):
            _show_failure(f, start + i, len(unlabeled))
            updated = label_one(f)
            ds.upsert(updated, dataset_path)
            labeled_count += 1
            print(f"  {_GREEN}✓ Saved{_RESET}   ({labeled_count} labeled this session)")

    except KeyboardInterrupt:
        print(f"\n\nPaused after labeling {labeled_count} records.")
        print("Run again to continue from where you left off.")
        return

    print(f"\n{_GREEN}Done. All {len(unlabeled)} unlabeled records are now labeled.{_RESET}")
    print(f"Next: python analyze.py")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Interactive labeling tool for the Rung 1 failure audit."
    )
    p.add_argument("--dataset", default=str(ds.DEFAULT_DATASET),
                   help="path to labeled_failures.jsonl")
    p.add_argument("--start", type=int, default=0,
                   help="skip this many unlabeled records (resume offset)")
    args = p.parse_args()

    run_labeling(Path(args.dataset), start=args.start)


if __name__ == "__main__":
    main()
