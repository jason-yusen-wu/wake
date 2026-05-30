# wake

**An incremental, demand-driven, error-tolerant value-flow analysis engine for coding agents.**

The bet: a single analysis engine, serving three modes (retrieval, generation-steering,
post-edit verification), can close part of the "last 20%" correctness gap that coding agents
hit when test coverage is thin. The wedge is **verification** — giving an agent a precise,
low-false-positive signal that its locally-plausible edit broke a non-local invariant.

See [`design.md`](design.md) for the full design rationale and the validation ladder.

---

## Status (honest)

The project follows a **validation ladder** (design.md §2) — buy information in increasing-cost
increments and let the project die or redirect early. Here is where each rung actually stands:

| Rung / Phase | What it tests | Result |
|---|---|---|
| **Rung 1** — failure audit (n=500) | Is the failure distribution analyzable? Which property? | ✅ **Pass.** 19% analyzable; `misunderstood_intent` only 2% (no kill); property redirect to **change_consistency** (73/93) over nullability (14/93). |
| **Rung 2** — oracle ceiling (n=93) | Does *perfect* feedback lift the agent? | ✅ **Pass.** +16.6% patch-coverage delta (oracle vs ablation); high-confidence subset +21.4%. The premise holds. |
| **Engine** — the real analyzer | Does wake's engine reproduce that signal on real code? | ❌ **Not yet.** On the 93 analyzable instances wake produces **0 correctly-located findings** (2 spurious fires). |
| **Phase 7 Part A** — daemon on controlled corpus | Does the machinery work? | ✅ **Pass.** 12/12 catch, 0% false-positive, 100% concrete witnesses. |
| **Phase 7 Part B** — closed loop | Does edit→verify→feedback→fix wire up? | ✅ Integration works; lift test degenerate (toy corpus saturates on Sonnet). |
| **Phase 8** — SWE-bench Verified lift | Real in-loop lift vs ablation | ⚙️ Harness built & smoke-tested (1 instance, both arms resolve). **Not run at scale** — gated on the engine gap below. |

### The engine gap (why we have not run Phase 8 at scale)

wake's **machinery** is sound — salsa-incremental IFDS, witness construction, the differential
layer, the daemon protocol, and the SWE-agent v1.1.0 closed loop all function. The problem is
**coverage of real bug mechanisms**:

- **Nullability sources** modeled: literal `x = None`, and (as of the latest fix) `None`-default
  and `Optional`-annotated parameters. **Not** modeled: library returns (`cursor.fetchone()`,
  `re.match()`, `dict.get()`), cross-object attribute state (`self.x`), or caller-supplied `None`
  to plain parameters.
- **Consumers** modeled: attribute / subscript / call dereference. **Not** modeled: comparison
  (`None < x`), argument-passing, arithmetic.
- **change_consistency** — the property the Rung-1 audit actually selected for 73/93 of the
  analyzable bucket — is **unimplemented**. The differential machinery exists but is hard-wired
  to the nullability lattice end-to-end (`NullRegression`, `null_regressions`, `RootCause` ∈
  {`NoneAssignment`, `NullableParam`, `Opaque`}). There is no change-consistency property.

Net: the controlled Phase 7 corpus (built to exercise the machinery) is 12/12; real SWE-bench
code is 0/93. The corpus never resembled real bugs, so it never surfaced this. **The ladder did
its job** — it revealed the engine investment needed *before* a thousand-dollar eval was spent.

The empirically-grounded build order to make wake useful on real code:
1. Library-return + cross-object-attribute `None` sources, plus comparison consumers
   (would move nullability from ~0 toward ~6–8/14).
2. Interprocedural caller-`None` propagation to plain parameters.
3. **change_consistency** as a first-class property (the 73-instance majority) — a from-scratch
   build across extractor → solver → diff → feedback, not a wire-up.

---

## Architecture — the narrow waist

```
Source ─► Extractor (per-language, M side) ─► FACT SCHEMA (the narrow waist)
                                                   │  defs, uses, def-use, CFG,
                                                   │  call edges, type facts,
                                                   │  value-flow nodes, + `Unknown`
                                                   ▼
                                          VALUE-FLOW ENGINE (shared, N side)
                                          IFDS/IDE solver, salsa-incremental
                                                   ▼
                                          PROPERTY LATTICES (nullability today)
                                                   ▼
                          retrieval │ generation │ differential (verification)
                                                   ▼
                                          FEEDBACK SHAPER ─► DAEMON (JSON-RPC/stdio)
                                                   ▼
                                          AGENT-LOOP HARNESS ─► SWE-bench eval
```

The **fact schema** (`wake-schema`) is the compatibility contract. Extractors populate it;
the engine queries it. The engine never knows about Python. `Unknown` is first-class — emit it
rather than a wrong answer (**precision over soundness**).

### Crates

| Crate | Role |
|---|---|
| `wake-schema` | Fact schema: `Def`, `Use`, `DefUseEdge`, `CfgEdge`, `CallEdge`, `TypeFact`, `Unknown`, the `LatticeDomain` trait, `NullabilityValue`. |
| `wake-extract-py` | Python extractor (tree-sitter → schema). The **only** language-specific crate. |
| `wake-ir` | Supergraph construction; CFG / def-use. |
| `wake-engine` | salsa db + IFDS/IDE solver scaffolding (property-agnostic). |
| `wake-prop-null` | Nullability lattice + interprocedural summaries. |
| `wake-diff` | Differential layer: blast radius + regressions + witnesses (currently nullability-specific). |
| `wake-feedback` | Feedback shaper: dedup, rank, minimal witness. |
| `wake-daemon` | JSON-RPC server (stdio) holding workspace state. |

### Clients & harnesses

| Path | Role |
|---|---|
| `clients/wake-py/wake_client.py` | Thin Python client for the daemon (4 RPCs). |
| `harness/agent-loop/` | Phase 7: CEGIS loop (`wake_harness.py`), controlled corpus eval (`corpus_eval.py`, Part A), in-loop lift (`loop_eval.py`, Part B). |
| `harness/eval/` | Phase 8: SWE-bench Verified runner (`batch_eval.py`) over SWE-agent v1.1.0, metrics (`metrics.py`), daemon-only false-positive sweep (`fp_sweep.py`). |
| `probe/audit/` | Rung 1: failure taxonomy (`collect.py`, `autolabel.py`, `analyze.py`) + labeled dataset. |
| `probe/oracle/` | Rung 2: Wizard-of-Oz oracle harness (`autorecord.py`, `harness.py`, `eval.py`). |

---

## Build & test

```bash
cargo build --release          # build (wake-daemon binary → target/release/wake-daemon)
cargo test                     # all Rust tests
cargo clippy                   # lint
```

## Running the evaluations

```bash
# Rung 1 + Rung 2 pipeline (needs ANTHROPIC_API_KEY)
bash run_all.sh --n 500 --skip-loop --workers 16

# Phase 7 Part A — daemon catch/FP gate, no API key, no Docker
python3 harness/agent-loop/corpus_eval.py --daemon target/release/wake-daemon

# Phase 8 — SWE-bench Verified, wake vs ablation (needs Docker + SWE-agent + key)
bash harness/eval/setup.sh                       # one-time: clone+install SWE-agent, pull images
python3 harness/eval/batch_eval.py \
    --filter-from-audit analyzable \             # scope to the Rung-1 analyzable subset
    --workers 4 --yes
python3 harness/eval/metrics.py                  # headline + stratified report
python3 harness/eval/fp_sweep.py                 # daemon-only FP measurement ($0)
```

`batch_eval.py` prints an expected-vs-cap cost/wall-time estimate and prompts before launching.
A run manifest (`harness/eval/reports/phase8_manifest.json`) is updated per arm so a killed run
leaves an accurate record.

### Notes on the toolchain

- Python is managed with **uv** (Python 3.11+); `harness/eval/setup.sh` auto-detects `uv`, `pip3`,
  or `pip`.
- SWE-agent **v1.1.0** is vendored to `harness/eval/swe-agent/` (git-ignored) and installed
  editable (its import-time assertions require the source tree). `batch_eval.py` patches two
  upstream seams: the agent factory (to attach `WakeHook`) and `CombinedAgentHook.on_setup_done`
  (an upstream dispatch bug where the setup callback never reaches attached hooks).

---

## Design principles (load-bearing)

- **Precision over soundness.** Emit `Unknown`, never a false positive. A noisy verifier destroys
  agent trust — empirically confirmed: wake's 2 spurious fires would *distract* the agent.
- **Decline to answer** on constructs that can't be reasoned about. Silence beats wrong.
- **Demand-driven.** The agent's query scopes the computation.
- **Tolerate broken code.** Partial programs are the default input.
- **Keep the seam clean.** Only `wake-extract-py` knows Python; schema/engine/property crates
  stay language-neutral.

## Repository data artifacts (committed)

- `probe/audit/corpus/labeled_failures.jsonl` — the Rung-1 labeled dataset (n=500), reusable.
- `probe/oracle/feedback/*.json` — Rung-2 oracle feedback (n=93), the inputs that define the
  lift ceiling.

Run outputs (results, caches, logs, the SWE-agent clone) are git-ignored and regenerable.

## License

Apache-2.0 (see [`LICENSE`](LICENSE)).
