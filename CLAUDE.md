# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**wake** is an incremental, demand-driven, error-tolerant value-flow analysis engine for coding agents. The core bet: a single engine serving three modes (retrieval, generation-steering, and post-edit verification) can close the "last 20%" correctness gap that coding agents hit when tests are thin. See `design.md` for the full design rationale.

## Commands

```bash
cargo build          # build
cargo test           # run all tests
cargo test <name>    # run a single test by name substring
cargo clippy         # lint
cargo run            # run the daemon (eventually)
```

## Planned crate structure

The project is not yet decomposed into crates. The target layout (from `design.md §6.2`):

```
crates/
  wake-schema/      # fact schema: Def, Use, DefUseEdge, CfgEdge, CallEdge, TypeFact, ValueFlowNode, Unknown
  wake-extract-py/  # Python extractor (tree-sitter → schema) — the only language-specific crate
  wake-ir/          # supergraph construction from schema; CFG/def-use
  wake-engine/      # salsa db + IFDS/IDE solver (property-agnostic)
  wake-prop-null/   # nullability lattice: {NonNull, Nullable, Unknown}
  wake-diff/        # differential layer: blast radius + regressions + witnesses
  wake-feedback/    # feedback shaper: dedup, rank, minimal witness
  wake-daemon/      # JSON-RPC server (stdio) holding workspace state
clients/
  wake-py/          # thin Python client SDK
harness/
  agent-loop/       # mandatory-gate CEGIS loop around a swappable model
  eval/             # SWE-bench Verified runner + metrics
probe/
  audit/            # Rung 1 failure taxonomy tooling + labeled dataset
  oracle/           # Rung 2 Wizard-of-Oz harness (human plays the analyzer)
```

## Architecture — the narrow waist

The fact **schema** (`wake-schema`) is the compatibility contract. Extractors (M side) populate it; the engine (N side) queries it. This M+N decomposition means the engine never knows about Python specifically.

Key schema relations: `Def`, `Use`, `DefUseEdge`, `CfgEdge(from, to, kind)`, `CallEdge(call_site, callee, confidence)`, `TypeFact(node, type, source)`, `ValueFlowNode(node, role)`, `Unknown(node, reason)`. The `Unknown` marker is first-class — emit it rather than a wrong answer.

The **engine** is an IFDS/IDE solver over the exploded supergraph, wrapped in **salsa** queries for demand-driven incremental recompute. Each expensive step (parse → schema → supergraph fragment → procedure summary → reachability) is a salsa query. Early-cutoff means an edit that doesn't change a procedure's summary doesn't invalidate its callers.

The **differential layer** diffs nullability facts at consumer nodes before/after an edit. Output: blast radius (changed-fact set) + regressions (newly-possible None-dereferences) + minimal witness paths.

The **daemon** holds indexed workspace state across edits (amortizes cold-start). Protocol is JSON-RPC over stdio. Key methods: `analyze/blastRadius`, `analyze/regressions`, `query/valueFlow`, `workspace/didChange`.

## Design principles (load-bearing)

- **Precision over soundness.** Emit `Unknown`, never a false positive. A noisy verifier destroys agent trust.
- **Decline to answer** on constructs that can't be reasoned about. Silence beats wrong.
- **Demand-driven.** The agent's query scopes the computation — never analyze the whole codebase.
- **Tolerate broken code.** Partial programs are the default input. tree-sitter error nodes → `Unknown` at the boundary.
- **Keep the seam clean.** `wake-schema`, `wake-ir`, `wake-engine`, `wake-prop-null`, `wake-diff`, `wake-feedback` must contain zero Python-specific concepts. Only `wake-extract-py` knows about Python.

## Build order / phases

Phases must be completed in order (each has a checkpoint gate):

- **Phase −1** (Probe): Rung 1 failure audit + Rung 2 oracle test. Do not write engine code until these pass.
- **Phase 0**: salsa spine + interner + arena. Gate: only affected queries recompute on edit.
- **Phase 1**: Schema + IR + intraprocedural value-flow. Gate: single-function demand query, incremental, survives syntax error elsewhere.
- **Phase 2**: Nullability intraprocedural. Gate: detect None-deref; no false positive on annotated path.
- **Phase 3**: Interprocedural IFDS + summaries. Gate: cross-function None-flow; summaries reused.
- **Phase 4**: Differential layer. Gate: regressing edit → correct witness; benign edit → empty (false-positive gate for the whole project).
- **Phases 5–8**: Feedback shaper → Daemon → Agent-loop harness → SWE-bench eval.

## Testing discipline

The single most important test: **differential-test the incremental path against a from-scratch oracle.** On a randomized edit stream, assert `incremental_result == scratch_result`. Stale facts from incorrect salsa invalidation are the highest-risk correctness bug class. Build this harness alongside the engine in Phase 0–1, not after.

Also maintain:
- Golden tests on the Rung-1 labeled regression corpus (true-positive recall)
- A benign-edit corpus for false-positive measurement (Phase 4 gate)
- Property-based tests for lattice transfer functions
