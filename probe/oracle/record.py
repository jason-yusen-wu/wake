"""
probe/oracle/record.py — guided CLI for writing oracle feedback.

For each analyzable instance from the Rung 1 labeled dataset, presents:
  • the problem statement
  • the patch (or gold patch)
  • the test failures
  • the Rung 1 label (category, property, analysis note)

Then asks the human to write the feedback that a *perfect* static analyzer
would provide — the message the agent will receive in the oracle CEGIS loop.

Guidelines for writing good oracle feedback:
  • Be specific: name the variable, the function, the line.
  • Format like wake's output: root cause first, then consumer sites.
  • Keep it under ~150 tokens (what a real shaped finding would say).
  • Do NOT tell the agent what fix to apply — describe the problem, not the solution.

Recorded feedback is stored in probe/oracle/feedback/<instance_id>.json.

Usage:
  python record.py
  python record.py --audit-dataset ../../probe/audit/corpus/labeled_failures.jsonl
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import textwrap
from pathlib import Path

FEEDBACK_DIR = Path(__file__).parent / "feedback"
AUDIT_DEFAULT = Path(__file__).parent.parent / "audit" / "corpus" / "labeled_failures.jsonl"

# ── Formatting ────────────────────────────────────────────────────────────────

_BOLD  = "\033[1m"
_CYAN  = "\033[36m"
_YELLOW= "\033[33m"
_GREEN = "\033[32m"
_RED   = "\033[31m"
_DIM   = "\033[2m"
_RESET = "\033[0m"

def _h(s: str) -> str:
    return f"{_BOLD}{_CYAN}{s}{_RESET}"

def _divider(width: int = 72) -> None:
    print(_DIM + "─" * width + _RESET)

FEEDBACK_TEMPLATE = """\
[HIGH] Root cause: '<VARIABLE>' is directly assigned None / param '<VARIABLE>' is Optional (can be None)
  • line <LINE>: '<VARIABLE>' used as attribute/subscript/call — <brief trace>
  Suggested fix location: line <LINE>
"""

GUIDELINES = """
Guidelines for oracle feedback:
  • Name the exact variable, function, and line number.
  • Root cause first (None assignment or Optional param).
  • Each consumer site on its own bullet with line number.
  • Confidence tag: [HIGH], [MEDIUM], or [LOW].
  • < 150 tokens. Describe the problem, not the fix.
  • Format mirrors wake's shaped output so the agent treats it like a real finding.
"""


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load_audit_failures(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _feedback_path(instance_id: str) -> Path:
    return FEEDBACK_DIR / f"{instance_id}.json"


def _load_feedback(instance_id: str) -> dict | None:
    p = _feedback_path(instance_id)
    return json.loads(p.read_text()) if p.exists() else None


def _save_feedback(fb: dict) -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    p = _feedback_path(fb["instance_id"])
    with open(p, "w") as f:
        json.dump(fb, f, indent=2)


# ── Display ───────────────────────────────────────────────────────────────────

def _show_case(f: dict, index: int, total: int) -> None:
    print()
    _divider(width=72)
    print(_h(f"[{index + 1}/{total}] {f['instance_id']}"))
    print(f"  Category: {f.get('category','')}   Property: {f.get('which_property','')}   "
          f"Source: {f.get('patch_source','')}")
    _divider()

    ps = f.get("problem_statement", "")
    print(_YELLOW + "Problem statement:" + _RESET)
    for line in ps.strip().splitlines()[:6]:
        print("  " + textwrap.shorten(line, 78))
    if len(ps.splitlines()) > 6:
        print(_DIM + "  … (truncated)" + _RESET)
    _divider()

    print(_YELLOW + "Rung 1 analysis note:" + _RESET)
    note = f.get("analysis_note") or "(none)"
    print(f"  {note}")
    _divider()

    patch = f.get("patch", "")
    if patch:
        print(_YELLOW + "Patch (first 60 lines):" + _RESET)
        for line in patch.splitlines()[:60]:
            col = _GREEN if line.startswith("+") else (_RED if line.startswith("-") else _DIM)
            rst = _RESET
            print(f"  {col}{line[:100]}{rst}")
        if len(patch.splitlines()) > 60:
            print(_DIM + "  … (truncated)" + _RESET)
    else:
        print(_DIM + "  (no patch recorded)" + _RESET)

    _divider()
    print(_YELLOW + "Tests:" + _RESET)
    test_results = f.get("test_results", {})
    ftp = f.get("fail_to_pass", [])
    ptp = f.get("pass_to_pass", [])
    failures = [t for t in ftp if test_results.get(t) != "PASSED"][:5]
    regressions = [t for t in ptp if test_results.get(t) == "FAILED"][:5]
    if failures:
        print(f"  {_RED}Held-out failures: {', '.join(failures)}{_RESET}")
    if regressions:
        print(f"  {_RED}Regressions:       {', '.join(regressions)}{_RESET}")
    if not failures and not regressions:
        print(_DIM + "  (gold mode — no test run)" + _RESET)
    print()


# ── Recording loop ────────────────────────────────────────────────────────────

def record_session(audit_path: Path, skip_existing: bool = True) -> None:
    failures = _load_audit_failures(audit_path)
    analyzable = [f for f in failures if f.get("would_catch") in ("yes", "partial")]

    if not analyzable:
        print("No analyzable failures found. Run probe/audit/label.py first.")
        return

    pending = (
        [f for f in analyzable if _load_feedback(f["instance_id"]) is None]
        if skip_existing else analyzable
    )

    print(_h("\nWake Rung 2 — Oracle Feedback Recording"))
    print(f"  Analyzable: {len(analyzable)}   Pending: {len(pending)}")
    print(GUIDELINES)
    print("  Press Ctrl-C to pause.  Run again to continue.\n")

    recorded = 0
    try:
        for i, f in enumerate(pending):
            _show_case(f, i, len(pending))

            # Show template
            print(_YELLOW + "Feedback template (edit freely):" + _RESET)
            print(_DIM + FEEDBACK_TEMPLATE + _RESET)

            # Multi-line input: user types feedback, ends with a single blank line.
            # Implementation: accumulate lines; stop when the user enters a blank
            # line after having typed at least one non-blank line.
            print(f"  {_BOLD}Enter oracle feedback{_RESET} (press Enter on a blank line to finish):")
            lines = []
            while True:
                try:
                    line = input("  ")
                except EOFError:
                    break
                if line == "" and any(l.strip() for l in lines):
                    break
                lines.append(line)
            feedback_text = "\n".join(lines).strip()

            if not feedback_text:
                print("  Skipped (no feedback entered).")
                continue

            conf_raw = input(f"  {_BOLD}Confidence{_RESET} (high/medium/low) [high]: ").strip().lower()
            conf = conf_raw if conf_raw in ("high", "medium", "low") else "high"

            fb = {
                "instance_id": f["instance_id"],
                "feedback_text": feedback_text,
                "category": f.get("category", ""),
                "which_property": f.get("which_property", ""),
                "confidence": conf,
                "is_analyzable": True,
                "gold_patch": f.get("patch", ""),
                "recorded_by": "human",
                "record_timestamp": datetime.datetime.utcnow().isoformat(),
            }
            _save_feedback(fb)
            recorded += 1
            print(f"  {_GREEN}✓ Saved → {_feedback_path(f['instance_id'])}{_RESET}")

    except KeyboardInterrupt:
        print(f"\n\nPaused after recording {recorded} feedback entries.")
        return

    print(f"\n{_GREEN}Done. {recorded} oracle feedback records saved.{_RESET}")
    print("Next: python eval.py")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Record oracle feedback for Rung 2 Wizard-of-Oz test."
    )
    p.add_argument("--audit-dataset", default=str(AUDIT_DEFAULT))
    p.add_argument("--re-record", action="store_true",
                   help="re-record even instances that already have feedback")
    args = p.parse_args()

    record_session(
        audit_path=Path(args.audit_dataset),
        skip_existing=not args.re_record,
    )


if __name__ == "__main__":
    main()
