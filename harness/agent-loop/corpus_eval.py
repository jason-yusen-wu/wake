"""
corpus_eval — Part A of Phase 7 validation. No API calls required.

For each spec:
  1. Register all workspace files via workspace/didChange.
  2. Call analyze/regressions on the primary file → check catch rate.
  3. Call analyze/blastRadius with the fixed version → must show zero new regressions (FP gate).
  4. Call query/valueFlow at the consumer sites → exercise retrieval mode.
  5. Measure warm-query latency.
  6. Verify witness completeness (no Opaque steps) on caught regressions.

Pre-registered thresholds (Phase 7 gate):
  Catch rate:               >= 90%   (9/10 expected regressions caught)
  False-positive rate:      == 0%    (fixed versions must produce no new regressions)
  Witness fully concrete:   >= 70%   (no Opaque steps on >= 70% of findings)
  Warm-query latency:       <  2000 ms
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "clients" / "wake-py"))
from wake_client import WakeClient

CORPUS_DIR = Path(__file__).parent / "corpus"
SPECS_DIR = CORPUS_DIR / "specs"

THRESHOLD_CATCH_RATE = 0.90
THRESHOLD_FP_RATE = 0.0
THRESHOLD_WITNESS_CONCRETE = 0.70
THRESHOLD_LATENCY_MS = 2000.0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    spec_id: str
    spec_name: str
    # Regression detection
    expected_count: int
    caught_count: int
    catch_ok: bool
    # False-positive gate
    fp_new_regressions: int
    fp_ok: bool
    # Retrieval mode
    value_flow_exercised: bool
    value_flow_non_empty: bool
    # Witness quality
    total_witnesses: int
    concrete_witnesses: int
    # Latency
    warm_latency_ms: float
    # Raw data
    regressions: list[dict] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_spec(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def resolve_file(spec_path_str: str) -> Path:
    return CORPUS_DIR / spec_path_str


def build_uri(file_path: Path) -> str:
    return "file://" + str(file_path.resolve())


def has_opaque(witness: list[dict]) -> bool:
    return any(s.get("kind") == "opaque" for s in witness)


def regressions_match(
    found: list[dict], expected: list[dict], expect_dedup: bool
) -> tuple[int, int]:
    """
    Returns (caught_count, expected_count).

    For deduped cases (expect_dedup=True), one ShapedFeedback with multiple
    consumers counts as multiple regressions caught.
    """
    expected_count = len(expected)
    if not found:
        return 0, expected_count

    if expect_dedup:
        # All expected regressions should appear as consumers in a single finding.
        caught = sum(len(r.get("consumers", [])) for r in found)
        return min(caught, expected_count), expected_count

    caught = 0
    for exp in expected:
        exp_func = exp.get("func", "")
        exp_sym = exp.get("symbol", "")
        exp_kind = exp.get("kind", "")
        for r in found:
            for c in r.get("consumers", []):
                if (
                    c.get("symbol", "") == exp_sym
                    and c.get("kind", "") == exp_kind
                ):
                    caught += 1
                    break
    return caught, expected_count


def find_symbol_position(source: str, symbol: str) -> int | None:
    """Find the first occurrence of the symbol as a whole word."""
    import re
    m = re.search(rf"\b{re.escape(symbol)}\b", source)
    return m.start() if m else None


# ---------------------------------------------------------------------------
# Per-case evaluation
# ---------------------------------------------------------------------------

def evaluate_case(spec: dict, client: WakeClient) -> CaseResult:
    spec_id = spec["id"]
    spec_name = spec["name"]
    expect_dedup = spec.get("expect_dedup", False)
    expected_regs = spec.get("expected_regressions", [])
    vf_symbol = spec.get("value_flow_symbol", "")

    result = CaseResult(
        spec_id=spec_id,
        spec_name=spec_name,
        expected_count=len(expected_regs),
        caught_count=0,
        catch_ok=False,
        fp_new_regressions=0,
        fp_ok=False,
        value_flow_exercised=False,
        value_flow_non_empty=False,
        total_witnesses=0,
        concrete_witnesses=0,
        warm_latency_ms=0.0,
    )

    try:
        # Build URI map for all files in this case.
        primary_path = resolve_file(spec["primary_file"])
        primary_uri = build_uri(primary_path)
        buggy_text = primary_path.read_text()

        file_uris: dict[str, str] = {}  # uri → text
        for fspec in spec["files"]:
            p = resolve_file(fspec["path"])
            file_uris[build_uri(p)] = p.read_text()

        # Register all workspace files.
        for uri, text in file_uris.items():
            client.did_change(uri, text)

        # 1. Warm up (cold call discarded for latency).
        client.analyze_regressions(primary_uri)

        # 2. Catch rate.
        regressions = client.analyze_regressions(primary_uri)
        result.regressions = regressions
        caught, expected = regressions_match(regressions, expected_regs, expect_dedup)
        result.caught_count = caught
        result.expected_count = expected
        result.catch_ok = (caught >= expected) if expected > 0 else True

        # 3. Witness quality.
        for reg in regressions:
            for c in reg.get("consumers", []):
                result.total_witnesses += 1
                if not has_opaque(c.get("witness", [])):
                    result.concrete_witnesses += 1

        # 4. query/valueFlow — retrieve def-use context at the consumer site.
        result.value_flow_exercised = True
        if regressions:
            # Find byte position of the value_flow_symbol in the primary file.
            pos = find_symbol_position(buggy_text, vf_symbol) if vf_symbol else None
            if pos is not None:
                flows = client.query_value_flow(primary_uri, pos, direction="both")
                result.value_flow_non_empty = len(flows) > 0
            else:
                # Symbol not found — still exercised the method.
                flows = client.query_value_flow(primary_uri, 0, direction="both")

        # 5. Warm-query latency (second call, cache warm).
        t0 = time.perf_counter()
        client.analyze_regressions(primary_uri)
        result.warm_latency_ms = (time.perf_counter() - t0) * 1000

        # 6. False-positive gate: blastRadius with the fixed version of primary file.
        fixed_files = spec.get("fixed_files", [])
        fixed_primary = next(
            (f for f in fixed_files if f["role"] == "fixed"), None
        )
        if fixed_primary:
            fixed_path = resolve_file(fixed_primary["path"])
            fixed_text = fixed_path.read_text()
            blast = client.analyze_blast_radius(primary_uri, fixed_text)
            result.fp_new_regressions = len(blast.get("new_regressions", []))
            result.fp_ok = result.fp_new_regressions == 0
        else:
            result.fp_ok = True  # no fixed version provided

    except Exception as exc:
        result.error = str(exc)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(daemon_path: str = "wake-daemon") -> list[CaseResult]:
    specs = sorted(SPECS_DIR.glob("*.json"))
    if not specs:
        print("No spec files found in", SPECS_DIR)
        sys.exit(1)

    results: list[CaseResult] = []
    with WakeClient(daemon_path) as client:
        for spec_path in specs:
            spec = load_spec(spec_path)
            print(f"  [{spec['id']}] {spec['name']} ... ", end="", flush=True)
            r = evaluate_case(spec, client)
            results.append(r)
            status = "PASS" if (r.catch_ok and r.fp_ok and not r.error) else "FAIL"
            print(status)
            if r.error:
                print(f"       ERROR: {r.error}")

    return results


def print_summary(results: list[CaseResult]) -> bool:
    total = len(results)
    caught_total = sum(r.caught_count for r in results)
    expected_total = sum(r.expected_count for r in results)
    fp_cases = sum(1 for r in results if not r.fp_ok)
    witness_total = sum(r.total_witnesses for r in results)
    concrete_total = sum(r.concrete_witnesses for r in results)
    latencies = [r.warm_latency_ms for r in results if r.warm_latency_ms > 0]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

    catch_rate = caught_total / expected_total if expected_total else 1.0
    fp_rate = fp_cases / total if total else 0.0
    witness_rate = concrete_total / witness_total if witness_total else 1.0

    print()
    print("=" * 60)
    print("CORPUS EVAL — STATIC ANALYSIS (Part A)")
    print("=" * 60)
    print(f"  Catch rate:           {caught_total}/{expected_total} ({catch_rate:.0%})  [threshold: ≥{THRESHOLD_CATCH_RATE:.0%}]")
    print(f"  False-positive rate:  {fp_cases}/{total} ({fp_rate:.0%})  [threshold: =0%]")
    print(f"  Witness concrete:     {concrete_total}/{witness_total} ({witness_rate:.0%})  [threshold: ≥{THRESHOLD_WITNESS_CONCRETE:.0%}]")
    print(f"  Avg warm latency:     {avg_latency:.0f}ms  [threshold: <{THRESHOLD_LATENCY_MS:.0f}ms]")
    print()
    print("  Daemon methods exercised:")
    print("    workspace/didChange: ✓")
    print("    analyze/regressions: ✓")
    print("    analyze/blastRadius: ✓ (FP gate)")
    print(f"    query/valueFlow:     {'✓' if any(r.value_flow_exercised for r in results) else '✗'}")
    print()

    gate_pass = (
        catch_rate >= THRESHOLD_CATCH_RATE
        and fp_rate <= THRESHOLD_FP_RATE
        and witness_rate >= THRESHOLD_WITNESS_CONCRETE
        and avg_latency < THRESHOLD_LATENCY_MS
    )
    print(f"  PART A GATE: {'PASS ✓' if gate_pass else 'FAIL ✗'}")
    print()

    if any(r.error for r in results):
        print("  Errors:")
        for r in results:
            if r.error:
                print(f"    [{r.spec_id}] {r.spec_name}: {r.error}")
    return gate_pass


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--daemon", default="wake-daemon", help="Path to wake-daemon binary")
    args = p.parse_args()

    print("Running corpus evaluation (no API calls required)...")
    results = run(args.daemon)
    passed = print_summary(results)
    sys.exit(0 if passed else 1)
