# Project Wake — Master Design & Build Document

**v1.0 · "ready to build" edition.** Supersedes the v0.1 slice doc.

*Working codename "Wake": the disturbance an edit propagates through a program's value-flow graph. Rename freely.*

---

## 0. How to read this document, and what "ready to build" means

You are ready to **start**, not to have finished designing. Treat "complete design before building" as a trap: the validation ladder (§2) and the engine itself will teach you things no amount of up-front design can. This document is organized so that *starting is safe* — the early, cheap steps de-risk the expensive ones, and the architecture keeps expensive commitments behind cheap, reversible seams.

Read §1–§2 before anything. Then §3–§6 are the build. §7 is the order. §8 is how you know it works. §9–§10 are the future you should design *toward* but not *for* yet. §11 is the syllabus. §12 records why each decision was made so you don't relitigate them at 2am. §13 is what might go wrong. §14 is how to pace yourself.

---

## 1. Thesis and the two invariants

### 1.1 The problem

Coding agents reach ~80% fast and stall on the last 20%: the locally plausible, globally wrong edit. The agent cannot reliably tell when it has broken a non-local invariant, and its primary feedback channel — a passing/failing test suite — is silent exactly where coverage is thin. This is a **correctness-signal** problem, and sound program analysis is structurally the right instrument for it.

### 1.2 The bet

A single incremental, demand-driven, error-tolerant value-flow engine can serve **all three stages of the agent loop** as different uses of the same facts. Two structural insights make this more than a slogan:

**Invariant across languages — the narrow waist is a fact schema, not a syntactic IR.** Analysis is *property-preserving abstraction*, not *semantics-preserving lowering*, so there is no universal analysis IR the way LLVM IR is a universal codegen IR. The M×N → M+N decomposition is achieved by a **relational fact schema** (the CodeQL / Glean / Doop tradition): each language has an *extractor* populating a common schema (def-use, call edges, value-flow nodes, type facts, points-to candidates) and the analysis is written *once* over that schema. Non-uniform front-end quality shows up as **fact density plus explicit `unknown` markers**, not as different analysis code — a weak front-end populates fewer facts and more unknowns; the same solver runs and degrades gracefully. This is the precision-over-soundness principle realized in the architecture.

**Invariant across loop-stages — incremental analysis of incomplete programs touches all three stages.** Static-vs-dynamic is the wrong axis. The right axis is *when, relative to authoring*: before (classical static, on the committed artifact), **during** (online, on the program-in-construction), after-running (dynamic). The "during" regime is a genuine third modality and it is what reaches the **generation** stage. The *same* engine, distinguished only by *when it runs* and *what is done with the verdict*:

- queried non-differentially → **retrieval** (semantic context);
- run synchronously with authoring, verdict used to steer/constrain/rank → **generation**;
- run differentially across a committed edit, verdict reported → **verification**.

The capability that unifies them is incremental analysis of *incomplete* programs (the principled foundation is incomplete-program semantics; the pragmatic foundation is error-tolerant best-effort analysis — see §11).

### 1.3 Scope philosophy

Build the smallest thing that proves the bet, behind seams that make the future additive. The expensive shared substrate (extractor → schema → solver → differential) is built once; properties, languages, and loop-stage modes are added at the cheap edges. Never build the second language; always keep the seam that admits it.

---

## 2. Validation before commitment — the ladder

Do not try to "be sure" of the premise by reasoning. Buy information in increasing-cost increments; let the project die early if it's going to. The premise is two claims: the *strategic* one (shaped correctness feedback moves agent success more than more context) and the *slice* one (the chosen property is a meaningful share of analyzable agent failures). They have different epistemic status and different rungs.

**Rung 1 — Failure audit (days; before any code).** Run your target agent on a sample of SWE-bench Verified. Collect failed patches, and especially patches that pass *visible* tests but fail *held-out* ones. Build a taxonomy of failure modes (null/type errors; wrong-but-plausible logic; missing edge cases; integration/boundary errors; **incomplete edits** — changed a function, missed callers; API misuse; misunderstood intent). For each, ask the decisive question: *would a precise static analysis have caught this, and which property?* Outputs: (a) the size of the analyzable bucket (rough test of the strategic claim), (b) **the property to build first**, (c) a labeled dataset you reuse for eval. Be prepared for the audit to point at **change-consistency** (blast-radius completeness) rather than nullability — that's a redirection, not a refutation, and your substrate already computes it. Be prepared for it to **kill** the project if the dominant bucket is "misunderstood intent." That is the cheapest possible place to learn that.

**Rung 2 — Oracle / Wizard-of-Oz feedback test (no engine).** Before building any analyzer, *you* play the analyzer: hand-label regressions on a set of agent patches and feed that hand-perfect feedback into the agent loop. Measure lift. If flawless feedback doesn't move the resolved rate, no automated analyzer will, and you've learned it without writing Rust. If it does, you've established the ceiling your real engine chases, and the project becomes "how close to the oracle can imperfect automation get" — a tractable engineering question.

**Rung 3 — Engine + real in-loop ablation (the project; §8).** Only this tests whether automated, imperfect, precision-capped feedback retains the oracle's lift. Appropriately the last and most expensive validation.

Each rung is ~an order of magnitude cheaper than the next. This *is* how you become sure enough to commit.

---

## 3. Architecture overview

Layered, with the fact schema as the narrow waist and three modes over one engine.

```
                 ┌─────────────────────────────────────────────┐
   Source ──►  Extractor (per-language, M side)  ──► populates    │
                 ▼                                                 │
            ┌──────────────────────────────────────────────┐      │
            │  FACT SCHEMA  (the narrow waist)               │      │
            │  defs, uses, def-use edges, call edges,        │      │
            │  CFG nodes/edges, type facts, value-flow nodes,│      │
            │  + first-class `unknown` markers               │      │
            └──────────────────────────────────────────────┘      │
                 ▼                                                 │
            VALUE-FLOW ENGINE  (shared, N side)                    │
            IFDS/IDE solver over the exploded supergraph,          │
            salsa-incremental, error-tolerant                      │
                 ▼                                                 │
            PROPERTY LATTICES (nullability first; pluggable)       │
                 ▼                                                 │
        ┌────────────────┬──────────────────┬───────────────────┐ │
        │ retrieval mode │ online/gen mode   │ differential mode │ │
        │ (non-diff)     │ (synchronous)     │ (verification)    │ │
        └────────────────┴──────────────────┴───────────────────┘ │
                 ▼                                                 │
            FEEDBACK SHAPER  (rank, dedup, minimal witness)        │
                 ▼                                                 │
            DAEMON + JSON-RPC PROTOCOL + thin clients              │
                 ▼                                                 │
            AGENT-LOOP HARNESS (mandatory gate / CEGIS)            │
                 ▼                                                 │
            EVAL HARNESS (SWE-bench Verified)                      │
```

### Design principles (load-bearing — revisit before any tradeoff)

- **Precision over soundness.** A noisy verifier is ignored or misleads. Low false-positive rate is the goal; emit `unknown`, never a false `bug`.
- **Decline to answer** on constructs you can't reason about. Silence beats a wrong answer.
- **Agent-shaped output.** Minimal, causal, ranked, token-budget-aware.
- **Demand-driven.** Compute the slice a query needs; the agent's question defines scope and bounds dynamism exposure.
- **Latency budget.** Warm-workspace query well under a few seconds → incrementality is non-negotiable.
- **Tolerate broken code.** Partial programs are the default input, not the exception.
- **Keep the seam clean.** Language-neutral schema/engine; language-specific extractor. Design the seam, never the second language.

---

## 4. Components

**Extractor (per-language front-end).** Parses source (tree-sitter) and populates the fact schema to whatever precision it can, declaring `unknown` where it cannot. This is the only language-specific component and where most per-language cost lives (name resolution, type binding, call-graph construction, dynamism). The M side of M+N.

**Fact schema (the narrow waist).** A stable relational schema: definitions, uses, def-use edges, intra/inter-procedural CFG, call edges (with `unknown`-target marking for unresolved dynamic dispatch), type facts (bind to PEP 484 annotations where present), and value-flow nodes. Versioned independently; it is the compatibility contract between extractors and the engine.

**Value-flow engine.** Property-agnostic IFDS/IDE solver over the exploded supergraph derived from the schema. Demand-driven (tabulation scoped to the query), memoized and invalidated by salsa, error-tolerant (produces sound-modulo-`unknown` facts on partial programs). The N side.

**Property lattices.** Pluggable instantiations. First and only for the slice: nullability `{NonNull, Nullable, Unknown}` (refine later). Sources: literals, `Optional[...]` returns, known-None-returning functions, missable dict/attr accesses. Consumers: dereferences, subscripts, attribute access, calls.

**Differential layer.** Snapshots pre-edit facts; on edit, triggers incremental recompute, diffs facts at consumers, computes the **blast radius** (changed-fact set) and **regressions** (newly-reachable None at a previously-safe consumer, or a consumer newly connected to a None source), with minimal witness paths.

**Online / generation mode.** The same engine run *synchronously with authoring* (per-edit or per-line granularity), with the verdict used to **steer** (inject facts into context), **constrain** (block invalid continuations — open-model/logit-access only), **re-rank** (generate-N-candidates, analyze each, pick — works via API), or **sharpen holes** (identify underspecification). Reaches the generation stage with no new engine capability.

**Feedback shaper.** Turns verdicts into agent-actionable messages: one per root cause, deduplicated across consumers sharing a source, ranked by confidence and proximity, with minimal witness and suggested fix locus. First-class, not formatting.

**Daemon + protocol + clients.** Persistent process holding indexed, memoized workspace state (amortizes cold-start indexing). JSON-RPC over stdio. Thin Python client first (to match the agent harness and SWE-bench). The protocol — not a library ABI — is the cross-ecosystem compatibility contract.

**Agent-loop harness.** Thin loop around a swappable API model (or an Aider hook): edit → verify → on regression feed shaped counterexample back → regenerate. The mandatory gate that makes lift measurable.

**Eval harness.** SWE-bench Verified runner with the visible/held-out partition and the three headline metrics (§8).

---

## 5. The preliminary slice

**In scope:** Python target; nullability (or whatever Rung 1 selects); demand-driven interprocedural value-flow; the differential layer (blast radius + regressions); feedback shaping; daemon + Python client; mandatory-gate harness; SWE-bench Verified ablation.

**Out of scope (defer, do not creep):** cross-language flow; soundness and the long tail of Python dynamism; any second property; the online/generation mode; synthesis/repair; the typed edit calculus; MCP packaging; multi-language. Each is a later *additive* expansion (§9).

**The killer demo to aim at:** agent makes a plausible edit, visible tests pass, verifier reports "your change means `user` can be `None` at this dereference — here is the path from the source," agent fixes it. If that works, the bet is validated.

---

## 6. Implementation details

### 6.1 Tech stack and rationale

- **Engine: Rust.** Because **salsa** (rust-analyzer's demand-driven incremental framework) is the most mature realization of the exact paradigm needed; because **rust-analyzer** is the reference for error-tolerant analysis of incomplete code (mirror its arena/index/interning patterns); and because the **single-static-binary + clean FFI/WASM/bindings** story resolves cross-ecosystem distribution at the daemon boundary. Cost: graph-heavy analysis fights the borrow checker until you adopt arena + integer-index handles — which you want anyway for memoization. *OCaml is defensible* (Infer/Frama-C/Flow precedent; Jane Street `incremental`) only if your fluency and analysis-writing ergonomics outweigh salsa's head-start; the design above the engine is language-agnostic.
- **Parsing:** tree-sitter + tree-sitter-python (incremental, error-recovering).
- **Incrementality:** salsa (query memoization, revision-based invalidation, early-cutoff).
- **Solver:** custom IFDS/IDE tabulation (the distributive fragment; see 6.4).
- **Protocol:** JSON-RPC over stdio (`lsp-server`-style or hand-rolled).
- **Client / harness / eval:** Python.
- **Model:** any API model behind a swappable client.

Licenses are compatible: salsa, tree-sitter, lsp-server are MIT/Apache-2.0.

### 6.2 Suggested repository / crate layout

```
wake/
  crates/
    wake-schema/      # the fact schema: types, relations, `unknown` markers, versioning
    wake-extract-py/  # Python extractor: tree-sitter -> schema (the M side)
    wake-ir/          # supergraph construction from schema; CFG/def-use
    wake-engine/      # salsa db + IFDS/IDE solver (property-agnostic, the N side)
    wake-prop-null/   # nullability lattice (pluggable property)
    wake-diff/        # differential layer: blast radius + regressions + witnesses
    wake-feedback/    # feedback shaper
    wake-daemon/      # JSON-RPC server holding workspace state
  clients/
    wake-py/          # thin Python client SDK
  harness/
    agent-loop/       # mandatory-gate CEGIS loop around a swappable model
    eval/             # SWE-bench Verified runner + metrics
  probe/
    audit/            # Rung 1 taxonomy tooling + labeled dataset
    oracle/           # Rung 2 Wizard-of-Oz harness
  docs/
```

Keep `wake-schema`, `wake-ir`, `wake-engine`, and the property crates strictly free of Python-specific concepts — that is the seam that buys cross-language extensibility (§9.3).

### 6.3 Fact schema sketch

Relations (illustrative; refine against Rung 1):

```
Def(node_id, symbol, kind)
Use(node_id, symbol)
DefUseEdge(def_node, use_node, confidence)
CfgEdge(from_node, to_node, kind)            # intraprocedural control flow
CallEdge(call_site, callee, confidence)      # confidence carries `unknown` for dynamic dispatch
TypeFact(node_id, type, source)              # source ∈ {annotation, inferred, unknown}
ValueFlowNode(node_id, role)                 # role ∈ {source, consumer, transfer}
Unknown(node_id, reason)                     # first-class declared ignorance
```

`confidence`/`Unknown` are how non-uniform extractor quality is expressed uniformly. The solver treats `unknown`-marked edges conservatively *with respect to precision* (it declines to assert), not soundness.

### 6.4 The IFDS/IDE solver and salsa integration

- Model the analysis as a distributive dataflow framework; build the exploded supergraph (nodes × dataflow facts) lazily, **demand-driven** via tabulation scoped to the query's source/sink. Reuse procedure **summaries** (the path-edge/summary-edge construction) across queries.
- Wrap each expensive step as a salsa **query**: parse → schema → supergraph fragment → summary → reachability. salsa memoizes results keyed by inputs and invalidates by revision; **early-cutoff** means an edit that changes a function body but not its computed summary does *not* invalidate dependents. This is what delivers the latency budget.
- **Caveat on distributivity:** pure IFDS covers distributive flow functions; per-variable nullability fits well, but **path-sensitivity is not distributive**. Decide early how much path-sensitivity to buy (IDE buys more than IFDS via edge functions; full path conditions exceed both). Default: start path-*insensitive*, accept the imprecision, revisit only if Rung-1 data demands it.

### 6.5 Error tolerance and incomplete programs

- tree-sitter yields a tree with error nodes on broken input; the extractor maps recoverable regions to facts and emits `Unknown` for unrecoverable ones. A function that doesn't parse contributes `Unknown` at its boundary, not a crash.
- Treat holes/unparsable regions as **conservative `Unknown` sources/sinks**: they neither assert a bug nor guarantee safety. This is the pragmatic stand-in for full incomplete-program semantics (Hazel-style; see §11) and is sufficient for the precision-first stance.

### 6.6 Differential mechanism

Pre-edit facts are the memoized salsa state. Apply edit → salsa recomputes only invalidated summaries → diff nullability facts at consumer nodes in the changed set. **Blast radius** = consumers whose facts changed. **Regression** = a consumer that flips to possibly-None, or is newly connected to a None source. **Witness** = shortest value-flow path from the introduced source to the consumer (thin-slicing-style minimization).

### 6.7 Protocol sketch

```
analyze/blastRadius   { workspace, edit }      -> { changedNodes[] }
analyze/regressions   { workspace, edit, property } -> { regressions[]: {consumer, witnessPath, confidence} }
query/valueFlow       { workspace, node, direction }  -> { nodes[] }   # retrieval mode
workspace/didChange   { edits[] }              -> ack                  # keep daemon warm
```

### 6.8 Testing strategy (do not skip)

- **Differential-test the incremental path against a from-scratch oracle.** The highest-risk correctness bug class is stale facts from incorrect salsa invalidation. Maintain a from-scratch solver; on a randomized stream of edits, assert incremental == scratch. This single harness will save the project.
- Golden tests on a curated regression corpus (from Rung 1) for true-positive recall.
- A **benign-edit corpus** for false-positive measurement — the Phase-4 gate on the whole project (§13).
- Property-based testing of the lattice transfer functions.

### 6.9 Performance / latency

Cold-start indexing happens once per workspace in the daemon; warm queries are demand-scoped and memoized. Target warm-query latency under a few seconds on SWE-bench-sized repos. Measure recompute cost as a fraction of edit size — it should be roughly proportional to the change, not the codebase. If not, the incrementality is broken; fix before proceeding.

---

## 7. Dependency order of parts

Strictly ordered; each phase yields a runnable artifact and a checkpoint. Do not start a phase before its predecessor's checkpoint passes.

- **Phase −1 — Probe.** Rung 1 audit (taxonomy, property selection, labeled dataset) and Rung 2 oracle test (does perfect feedback lift?). **Gate: the strategic claim survives and a property is chosen.** If not, stop or redirect.
- **Phase 0 — Skeleton + salsa spine.** Workspace, salsa db, interner, arena; tree-sitter parses a file; a trivial incremental query recomputes on edit. *Checkpoint: only affected queries recompute.*
- **Phase 1 — Schema + IR + intraprocedural value-flow.** Extractor populates the schema; build supergraph + def-use; intraprocedural reaching-defs/value-flow as salsa queries; error-tolerant. *Checkpoint: single-function demand query correct, incremental, survives a syntax error elsewhere.*
- **Phase 2 — Nullability (intraprocedural).** Lattice + sources/consumers; use annotations. *Checkpoint: detect an intraprocedural None-deref; no false positive on an annotated-non-None path.*
- **Phase 3 — Interprocedural via demand-driven summaries.** IFDS tabulation + summary reuse, demand-scoped, salsa-memoized. *Checkpoint: cross-function None-flow detected; summaries reused; recompute touches only affected summaries.*
- **Phase 4 — Differential layer + blast radius.** *Checkpoint: regressing edit → correct minimal witness; benign edit → empty (measure false-positive rate here — project gate).*
- **Phase 5 — Feedback shaper.** *Checkpoint: one well-formed message per root cause on a multi-consumer regression.*
- **Phase 6 — Daemon + protocol + Python client.** *Checkpoint: warm-query latency in budget; cold-start amortized.*
- **Phase 7 — Agent-loop harness (mandatory gate).** *Checkpoint: on a hand-built regressing case, the loop catches, feeds back, the agent fixes.*
- **Phase 8 — Eval.** SWE-bench Verified, with/without gate, visible/held-out partition. *Checkpoint: the three metrics computed and reproducible.*

**Critical path:** Phases 0–3 are the high-risk core; 5–7 are engineering. If time compresses, Phases 0–4 plus a *manual* eval on the Rung-1 corpus already prove the engine and the differential idea; Phases 7–8 prove the *lift* (the strategic payoff) — skip only under duress.

---

## 8. Evaluation

**Benchmark:** SWE-bench Verified. **Ablation:** same agent and model, plain vs. mandatory gate.

1. **Resolved-rate delta** — does the gate raise correct-resolution rate? Headline lift.
2. **Regression-catch rate** — partition tests into *visible* (agent sees) and *held-out*; measure catches of held-out breaks the visible tests miss. Operationalizes the 80/20 claim: *caught what tests did not.*
3. **False-positive rate** — flags on correct patches. The trust metric; justifies precision-over-soundness.

**Secondary:** warm-query latency; recompute cost vs. edit size (validates incrementality); fraction answered vs. `unknown` (coverage honesty).

**Discipline:** pre-register thresholds and the visible/held-out policy before running. Some tasks won't partition cleanly — curate.

---

## 9. Extensions roadmap (post-slice, additive by construction)

**9.1 New properties (cheapest).** Taint, must-not-be-empty, resource lifetimes, units, custom contracts — each a new lattice over the existing engine. Taint ≈ nullability machinery with different sources/sinks.

**9.2 Generation-stage / online mode (the third mode).** Run the engine synchronously with authoring; use the verdict to steer/constrain/re-rank/sharpen-holes (§4). The cheapest first probe is **generate-N-and-rank**: no logit access, no new engine capability, measurable on the Rung-1 set (does analysis-based re-ranking beat the model's own ranking?). Constraining the decode requires open models; steering via context works via API.

**9.3 Cross-language via the waist (the expensive one — but only because of extractors).** Add front-ends that populate the *same* schema; add **boundary models** (serialization/RPC/ORM as value-flow edges) to track flow across language seams. The analysis is reused; the cost is the new extractor and the boundary modeling. This is *only* cheap if the engine/schema stayed language-neutral (§6.2) — the single most consequential extensibility decision, made in Phase 1.

**9.4 Synthesis / repair (mostly free given the verifier).** A CEGIS repair loop is "candidate generator + oracle"; your differential verifier *is* the oracle and the incremental per-edit checking already exists. Hole-filling, typed edit operations, and repair sit on top of the substrate.

---

## 10. Production and open-source plan

**What "production-hardening" means (the slice legitimately punts these):** precision/soundness tuning per property; performance and memory at monorepo scale (demand-driven helps; salsa-state persistence across daemon restarts and on-disk caching need engineering); robustness of error-tolerance across the long tail; multi-tenant/concurrent workspaces; observability (why did it flag / why `unknown`).

**Open-source architecture.** The daemon + versioned protocol is the public contract; extractors and property lattices are the natural contribution surface (the community adds languages and properties without touching the engine). Keep `wake-schema` and the protocol stable and well-documented — they are the API everyone builds against. Ship a single static binary plus the thin Python client; publish the protocol spec.

**Licensing.** Recommend **Apache-2.0** (permissive + explicit patent grant, the norm for dev infrastructure, maximizes adoption and downstream embedding). Choose copyleft only if preventing proprietary forks matters more than adoption. Dependencies (salsa, tree-sitter) are permissive, so no conflict either way.

**Governance / contribution.** Document the extractor interface and the `unknown` discipline as a contributor guide; provide an extractor conformance test suite against the schema so new languages are verifiable. Provide a property-author guide (how to define a lattice + sources/sinks). A reproducible eval harness is itself a community asset and a credibility signal.

**Adoption path.** Framework hook first (proves lift, mandatory gate). Then MCP packaging for reach across MCP-capable clients — but only after lift is demonstrated, since MCP's pull model lets agents skip an optional verifier.

---

## 11. Resources to learn (ordered path)

**Interprocedural & demand-driven dataflow (core algorithms)**
- Reps, Horwitz, Sagiv — *Precise Interprocedural Dataflow Analysis via Graph Reachability* (POPL 1995). IFDS. Read first.
- Sagiv, Reps, Horwitz — *Precise Interprocedural Dataflow Analysis with Applications to Constant Propagation* (TCS 1996). IDE, beyond the distributive fragment.
- Horwitz, Reps, Sagiv — *Demand Interprocedural Dataflow Analysis* (FSE 1995). The demand-driven formulation matching the agent-query model.
- Sridharan & Bodík — refinement-based / demand-driven points-to for Java (OOPSLA 2005 / PLDI 2006). Precise demand-driven at scale.
- Sridharan, Fink, Bodík — *Thin Slicing* (PLDI 2007). Minimal relevant slices — directly relevant to witness minimization and feedback shaping.
- Cousot & Cousot — *Abstract Interpretation* (POPL 1977). The lattice/soundness framing.

**The relational waist (cross-language extensibility)**
- CodeQL documentation — per-language extractors + shared dataflow library; the production exemplar of the fact-schema waist.
- Doop (Datalog points-to) and the Soufflé Datalog engine — analysis-as-relations.
- Glean (Meta) — language-neutral fact storage for code.
- SCIP (Sourcegraph) and LSIF (LSP indexing) — production neutral schemas for cross-language navigation; the shallow existence proof.

**Incrementality (latency requirement)**
- The **salsa** crate docs and design notes; rust-analyzer's in-repo `architecture.md`; Niko Matsakis's talks on responsive/incremental compilers.
- IncIDFA — *Efficient and Generic Algorithm for Incremental Iterative Dataflow Analysis* (OOPSLA 2025).
- GitHub Next — *Incremental CodeQL* writeup (what's hard, why).

**Parsing & error tolerance**
- tree-sitter docs (incremental parsing, error recovery) and tree-sitter-python.

**Incomplete-program semantics (the generation-stage foundation)**
- Hazel / Hazelnut (Omar et al., e.g. *Hazelnut: A Bidirectionally Typed Structure Editor Calculus*, POPL 2017) — typed holes; every intermediate editing state has meaning. The principled basis for "analyze code as it is written."
- Constrained / grammar-constrained decoding literature — the strong (logit-access) form of generation-stage steering.

**Python analysis prior art (pragmatism without soundness)**
- mypy, Pyright, Pyre, pytype — gradual-type handling and `Optional` treatment; aim for their pragmatism.
- Practical Python call-graph construction work (PyCG-style) — engineering compromises for dynamic dispatch.

**Agent loop & verification framing**
- SWE-bench (Jimenez et al., ICLR 2024) and SWE-bench Verified.
- CEGIS-with-LLM-as-generator: VeriGuard, PREFACE, recent agentic-verification work (LLM proposes invariants, tool checks). Read for *loop structure and feedback design*, not the Dafny specifics — the open space is mainstream-language, repo-scale, edit-loop verification with good feedback shaping.

---

## 12. Design decisions and rationale (so you don't relitigate)

- **Verification/feedback as the wedge, not retrieval.** Retrieval (LSP→MCP) is commoditizing; the 80/20 gap is a correctness-signal problem where analysis has an unfair advantage. Retrieval comes free as a query mode.
- **One engine, three modes.** Maximizes synergy and minimizes surface area; the shared substrate is the moat.
- **Fact schema, not syntactic IR, as the waist.** Analysis is property-preserving abstraction; no universal analysis IR exists. Relations + `unknown` absorb non-uniform extractor quality.
- **Precision over soundness.** Agent trust is the binding constraint; a noisy verifier is worse than none.
- **Rust + salsa.** Incrementality is non-negotiable and salsa is its best realization; single-binary distribution solves the compatibility worry.
- **Python first (pending Rung 1).** Max relevance + benchmark availability + max marginal value (the language gives the agent little). Tension acknowledged: Python is the *hardest* front-end; if Rung 1 shows the value lives elsewhere, the neutral seam makes switching additive. (A typed language like Go would validate the *engine* more cheaply but offers lower marginal value.)
- **Mandatory gate before MCP.** To *measure* lift you need verification to be non-optional; MCP's pull model permits skipping.
- **Defer cross-language and generation-stage** until the slice proves the bet — but keep the seams (neutral schema; incremental partial-program engine) that make both additive.

---

## 13. Risks and open questions

- **Python dynamism caps precision.** Mitigation: demand-scoping, annotations, decline-to-answer. Measure how often `unknown` dominates real code in Phases 2–3.
- **Non-distributive nullability with path conditions.** Decide path-sensitivity budget early; default path-insensitive.
- **False-positive rate is make-or-break.** Phase-4 benign-edit checkpoint gates the whole project. If high, agents won't defer.
- **Incremental correctness under salsa.** Stale-fact bugs from bad invalidation. The from-scratch differential-test harness (§6.8) is mandatory insurance.
- **Eval honesty.** The visible/held-out partition must make "held-out break" meaningful; curate.
- **The premise itself.** Rungs 1–2 exist precisely to kill or redirect cheaply before Phase 0.

---

## 14. Suggested cadence for a solo builder

- **Week 1:** Rung 1 audit. Build the taxonomy tooling, label ~100–200 agent failures, choose the property. Do *not* write engine code yet.
- **Week 2:** Rung 2 oracle test. Decide go/redirect/stop. This is your last cheap exit.
- **Weeks 3–4:** Phase 0–1 (salsa spine, schema, intraprocedural value-flow + the from-scratch oracle harness in parallel — build it *with* the engine, not after).
- **Weeks 5–7:** Phase 2–3 (nullability, demand-driven interprocedural). The hard core.
- **Week 8:** Phase 4 (differential + the false-positive gate). Decide whether to continue based on the FP rate.
- **Weeks 9–10:** Phases 5–7 (shaper, daemon, harness).
- **Weeks 11–12:** Phase 8 (eval). Write up the lift result.

Timeboxes are illustrative; the *order* and the *gates* are not. Stop at any failed gate and reassess rather than pushing through — the gates are where the project's honesty lives.

---

*End v1.0. The next artifact you'll likely want is the Rung-1 audit protocol (taxonomy categories, the visible/held-out mechanics, and the "would analysis have caught this, which property" labeling rubric), since the audit is now literally the first thing to build.*
