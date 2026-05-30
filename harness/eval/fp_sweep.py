"""
fp_sweep — daemon-only false-positive measurement on non-analyzable instances.

Pairs with Phase 8 restricted to the analyzable subset.  Phase 8 measures lift
where wake's instrument applies; this sweep measures wake's silence on the
~407 non-analyzable instances where it should NOT fire.

Method (no API calls; ~$0):
  For each non-analyzable instance:
    1. Identify the primary Python file modified by the gold patch.
    2. Fetch the buggy (pre-fix) version from GitHub at base_commit
       (reusing probe/oracle/cache/ when available).
    3. Register the file with wake-daemon and call analyze/regressions.
    4. Record:  fired=True iff len(regressions) > 0.

A non-analyzable instance is one the Rung-1 audit labeled would_catch=no
("definitively outside what nullability + change-consistency analysis can
catch").  Any regression wake fires on such instances is a false positive —
wake should be silent here by construction.

Headline: silence rate = 1 - (fired_count / n_fetched).  Combined with the
analyzable-arm lift, this gives the design doc's two trust signals:

  lift                                       (from Phase 8 on analyzable)
  silence on non-analyzable / FP rate        (from this sweep)

Usage:
  python fp_sweep.py --daemon path/to/wake-daemon

Output:
  reports/fp_sweep.json    machine-readable per-instance verdicts
  reports/fp_sweep.txt     human-readable summary
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "clients" / "wake-py"))
sys.path.insert(0, str(REPO_ROOT / "harness" / "eval"))
from wake_client import WakeClient, RpcError

AUDIT_DATASET = REPO_ROOT / "probe" / "audit" / "corpus" / "labeled_failures.jsonl"
# Reuse the Rung 2 source-file cache so we don't re-fetch what's already on disk.
CACHE_DIR = REPO_ROOT / "probe" / "oracle" / "cache"
REPORTS_DIR = Path(__file__).parent / "reports"

DEFAULT_DAEMON = REPO_ROOT / "target" / "release" / "wake-daemon"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class FpResult:
    instance_id: str
    repo: str
    category: str                       # Rung-1 category label
    filepath: str = ""                  # primary file analyzed
    fetched: bool = False
    fired: bool = False                 # ← the FP signal
    n_regressions: int = 0
    confidences: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Source-file fetch (mirrors probe/oracle/harness.py)
# ---------------------------------------------------------------------------

def parse_modified_py_files(patch: str) -> list[str]:
    """Python files modified (not deleted) by the gold patch, in order."""
    files: list[str] = []
    lines = patch.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("--- a/") and line != "--- a/dev/null":
            path = line[6:]
            if not path.endswith(".py"):
                continue
            plus = lines[i + 1] if i + 1 < len(lines) else ""
            if plus.startswith("+++ b/") and plus != "+++ b/dev/null":
                if path not in files:
                    files.append(path)
    return files


def _fetch(repo: str, base_commit: str, filepath: str, instance_id: str) -> str:
    """GET (with on-disk cache) the file at base_commit; return '' on failure."""
    safe = re.sub(r"[/\\]", "__", filepath)
    cache = CACHE_DIR / instance_id / safe
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="replace")
    url = f"https://raw.githubusercontent.com/{repo}/{base_commit}/{filepath}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wake-fp-sweep/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(content, encoding="utf-8")
        return content
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Per-instance probe
# ---------------------------------------------------------------------------

def probe_instance(record: dict, client: WakeClient) -> FpResult:
    iid = record["instance_id"]
    result = FpResult(
        instance_id=iid,
        repo=record.get("repo", ""),
        category=record.get("category", ""),
    )
    files = parse_modified_py_files(record.get("patch", ""))
    if not files:
        result.error = "no .py files in patch"
        return result
    fp = files[0]
    result.filepath = fp
    content = _fetch(record.get("repo", ""), record.get("base_commit", ""), fp, iid)
    if not content:
        result.error = "fetch failed"
        return result
    result.fetched = True

    uri = f"file:///fp_sweep/{iid}/{fp}"
    try:
        client.did_change(uri, content)
        regs = client.analyze_regressions(uri)
    except RpcError as exc:
        result.error = f"RpcError: {exc}"
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.n_regressions = len(regs)
    result.fired = len(regs) > 0
    result.confidences = [r.get("confidence", "?") for r in regs]
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Daemon-only FP sweep: how often does wake fire on "
                    "non-analyzable instances where it should be silent?"
    )
    p.add_argument("--daemon", default=str(DEFAULT_DAEMON))
    p.add_argument("--audit-dataset", default=str(AUDIT_DATASET))
    p.add_argument("--verdict", default="non_analyzable",
                   choices=["non_analyzable", "analyzable", "any"],
                   help="Which audit verdict to sweep (default: non_analyzable)")
    p.add_argument("--limit", type=int, default=None,
                   help="Only probe N instances (for fast iteration)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel fetch + daemon-RPC workers")
    p.add_argument("--output-dir", default=str(REPORTS_DIR))
    args = p.parse_args()

    if not Path(args.daemon).exists():
        print(f"ERROR: wake-daemon not found at {args.daemon}")
        sys.exit(1)

    audit_path = Path(args.audit_dataset)
    if not audit_path.exists():
        print(f"ERROR: audit dataset not found at {audit_path}")
        sys.exit(1)

    with open(audit_path) as fh:
        all_records = [json.loads(line) for line in fh if line.strip()]

    if args.verdict == "non_analyzable":
        records = [r for r in all_records if r.get("would_catch") == "no"]
    elif args.verdict == "analyzable":
        records = [r for r in all_records if r.get("would_catch") in ("yes", "partial")]
    else:
        records = [r for r in all_records if r.get("would_catch")]

    if args.limit:
        records = records[:args.limit]

    print(f"Sweeping {len(records)} '{args.verdict}' instances at workers={args.workers} ...")
    print(f"  (verdict='non_analyzable' is the FP measurement: wake should stay silent here)")

    # One daemon per worker is overkill — single daemon, thread-safe client.
    results: list[FpResult] = []
    t0 = time.perf_counter()
    with WakeClient(args.daemon) as client:
        def _work(r): return probe_instance(r, client)
        if args.workers <= 1:
            for r in records:
                results.append(_work(r))
                if len(results) % 20 == 0:
                    print(f"  ... {len(results)}/{len(records)}")
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futs = {ex.submit(_work, r): r for r in records}
                for f in as_completed(futs):
                    results.append(f.result())
                    if len(results) % 20 == 0:
                        print(f"  ... {len(results)}/{len(records)}")
    wall = time.perf_counter() - t0

    n = len(results)
    fetched = sum(1 for r in results if r.fetched)
    fired = sum(1 for r in results if r.fired)
    errors = sum(1 for r in results if r.error)
    high_conf = sum(1 for r in results if "high" in r.confidences)
    medium_conf = sum(1 for r in results if "medium" in r.confidences)

    fp_rate = fired / fetched if fetched else float("nan")

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_out = out / f"fp_sweep_{args.verdict}.json"
    json_out.write_text(json.dumps({
        "verdict": args.verdict,
        "n_total": n,
        "n_fetched": fetched,
        "n_fired": fired,
        "n_errors": errors,
        "fp_rate": fp_rate,
        "wall_time_s": wall,
        "results": [r.__dict__ for r in results],
    }, indent=2))

    txt_out = out / f"fp_sweep_{args.verdict}.txt"
    lines = [
        f"FP SWEEP — verdict={args.verdict}",
        "=" * 60,
        f"  Total records:        {n}",
        f"  Source fetched:       {fetched}/{n}",
        f"  Errors:               {errors}",
        f"  Wall time:            {wall:.1f}s",
        "",
        f"  Wake fired on:        {fired}/{fetched}  "
        f"({fp_rate:.1%}" + (" ← FP rate)" if args.verdict == "non_analyzable" else ")"),
        f"    HIGH-confidence:    {high_conf}",
        f"    MEDIUM-confidence:  {medium_conf}",
    ]
    if fired > 0:
        lines.extend(["", "  Instances where wake fired:"])
        for r in sorted([r for r in results if r.fired],
                        key=lambda r: -len([c for c in r.confidences if c == "high"]))[:20]:
            confs = ",".join(sorted(set(r.confidences)))
            lines.append(f"    {r.instance_id:48s} [{confs}]  cat={r.category}")
        if fired > 20:
            lines.append(f"    ... and {fired - 20} more")
    lines.append("")
    if args.verdict == "non_analyzable":
        lines.extend([
            "  INTERPRETATION",
            "  " + "-" * 40,
        ])
        if fp_rate < 0.05:
            lines.append(f"  ✓ FP rate {fp_rate:.1%} — wake is silent on non-analyzable bugs as designed.")
        elif fp_rate < 0.15:
            lines.append(f"  ⚠ FP rate {fp_rate:.1%} — wake fires on some non-analyzable bugs;")
            lines.append(f"    inspect the fired instances above to check for genuine FPs vs lucky catches.")
        else:
            lines.append(f"  ✗ FP rate {fp_rate:.1%} — wake fires too often on bugs outside its scope.")
            lines.append(f"    Precision-over-soundness invariant is at risk; review high-conf firings.")
    lines.append("=" * 60)
    txt_out.write_text("\n".join(lines))

    print()
    print("\n".join(lines))
    print(f"\nReports written:\n  {txt_out}\n  {json_out}")


if __name__ == "__main__":
    main()
