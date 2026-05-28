use std::collections::HashMap;
use wake_engine::{Db, SourceFile};
use wake_schema::{
    CallArgKind, ConsumerKind, NodeId, NullFact, NullFileFacts, NullFunctionFacts, NullReturn,
    NullRegression, NullabilityValue, RhsNullability,
};
use wake_prop_null::{FuncSummary, null_regressions, null_summaries};

const MAX_WITNESS_DEPTH: usize = 8;

// ── Public types ──────────────────────────────────────────────────────────────

/// One step in the backward trace explaining why a variable is Nullable.
/// Steps are ordered source-first, consumer-last.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub enum WitnessStep {
    /// The variable is a parameter that can be None (Nullable annotation or caller-supplied).
    NullableParam { node: NodeId, symbol: String },
    /// The variable was directly assigned `None`.
    NoneAssignment { node: NodeId, symbol: String },
    /// The variable was copied from a Nullable source: `to = from`.
    VariableCopy { node: NodeId, from: String, to: String },
    /// The variable was assigned the return value of a call that can return None.
    CallReturn { node: NodeId, callee: String, to: String },
    /// The terminal step: the Nullable variable is consumed here (None-dereference).
    Consumer { node: NodeId, symbol: String, kind: ConsumerKind },
    /// We cannot trace further (depth limit, cross-file call, opaque expression).
    Opaque { symbol: String },
}

/// A regression paired with the backward witness trace explaining it.
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct RegressionReport {
    pub regression: NullRegression,
    /// Backward trace: source-first, Consumer step last.
    pub witness: Vec<WitnessStep>,
}

/// Result of diffing two sets of regression reports.
#[derive(Clone, Debug, PartialEq, Eq, Default)]
pub struct DiffResult {
    /// Consumer nodes whose Nullable status changed (symmetric difference).
    pub blast_radius: Vec<NodeId>,
    /// Regressions present in `after` but not in `before`.
    pub new_regressions: Vec<RegressionReport>,
    /// Regressions present in `before` but not in `after`.
    pub fixed_regressions: Vec<NullRegression>,
}

// ── Salsa tracked ─────────────────────────────────────────────────────────────

/// Compute nullability regressions with backward witness traces for all functions in `file`.
/// Memoized per file; changing another file does not trigger recomputation here.
#[salsa::tracked]
pub fn regressions_with_witnesses(db: &dyn Db, file: SourceFile) -> Vec<RegressionReport> {
    let file_facts = wake_extract_py::extract_null_file(db, file);
    let regressions = null_regressions(db, file);
    let summaries = null_summaries(db, file);

    let summary_map: HashMap<String, &FuncSummary> =
        summaries.entries.iter().map(|(n, s)| (n.clone(), s)).collect();

    let mut reports = Vec::new();
    for (_bucket, regs) in &regressions {
        for reg in regs {
            // Locate the function that *contains* the consumer via the
            // regression's own `func_node` (the callee for interprocedural
            // regressions), so cross-function derefs get a real witness.
            let func = file_facts.functions.iter().find(|f| f.func_node == reg.func_node);
            let witness = func
                .map(|f| compute_witness(f, reg, &file_facts, &summary_map))
                .unwrap_or_default();
            reports.push(RegressionReport { regression: reg.clone(), witness });
        }
    }
    reports
}

// ── Pure functions ────────────────────────────────────────────────────────────

/// Position-independent identity of a regression: which function, which
/// variable, which kind of dereference, and which occurrence among siblings.
/// Deliberately excludes raw byte offsets so that an edit which merely shifts
/// text (a comment, an unrelated line) does not make an unchanged regression
/// look new or fixed.
type DiffKey = (String, String, ConsumerKind, usize);

/// Assign a stable `DiffKey` to each report. The occurrence ordinal is taken
/// from the relative byte order of consumers sharing the same
/// (function, symbol, kind), which is invariant under offset-only edits.
fn keyed(reports: &[RegressionReport]) -> HashMap<DiffKey, &RegressionReport> {
    let mut order: Vec<usize> = (0..reports.len()).collect();
    order.sort_by(|&i, &j| {
        let a = &reports[i].regression;
        let b = &reports[j].regression;
        a.func_name
            .cmp(&b.func_name)
            .then_with(|| a.object_symbol.cmp(&b.object_symbol))
            .then_with(|| a.kind.cmp(&b.kind))
            .then_with(|| a.consumer_node.start_byte.cmp(&b.consumer_node.start_byte))
            .then_with(|| a.consumer_node.end_byte.cmp(&b.consumer_node.end_byte))
    });

    let mut map = HashMap::new();
    let mut ordinal = 0usize;
    let mut prev: Option<(&str, &str, ConsumerKind)> = None;
    for &i in &order {
        let r = &reports[i].regression;
        let group = (r.func_name.as_str(), r.object_symbol.as_str(), r.kind);
        if prev == Some(group) {
            ordinal += 1;
        } else {
            ordinal = 0;
            prev = Some(group);
        }
        let key = (r.func_name.clone(), r.object_symbol.clone(), r.kind, ordinal);
        map.insert(key, &reports[i]);
    }
    map
}

/// Diff two snapshots of regression reports.
///
/// `before` and `after` are the results before and after an edit. Returns the
/// blast radius (consumer nodes whose Nullable status changed), newly appearing
/// regressions, and regressions that disappeared. Comparison is by position-
/// independent `DiffKey`, so benign offset-shifting edits produce an empty diff.
pub fn diff_results(before: &[RegressionReport], after: &[RegressionReport]) -> DiffResult {
    let before_keyed = keyed(before);
    let after_keyed = keyed(after);

    let mut new_regressions: Vec<RegressionReport> = after_keyed
        .iter()
        .filter(|(k, _)| !before_keyed.contains_key(*k))
        .map(|(_, r)| (*r).clone())
        .collect();

    let mut fixed_regressions: Vec<NullRegression> = before_keyed
        .iter()
        .filter(|(k, _)| !after_keyed.contains_key(*k))
        .map(|(_, r)| r.regression.clone())
        .collect();

    let mut blast_radius: Vec<NodeId> = new_regressions
        .iter()
        .map(|r| r.regression.consumer_node)
        .chain(fixed_regressions.iter().map(|r| r.consumer_node))
        .collect();
    blast_radius.sort();
    blast_radius.dedup();

    // Deterministic output ordering.
    new_regressions.sort_by_key(|r| (r.regression.consumer_node.start_byte, r.regression.consumer_node.end_byte));
    fixed_regressions.sort_by_key(|r| (r.consumer_node.start_byte, r.consumer_node.end_byte));

    DiffResult { blast_radius, new_regressions, fixed_regressions }
}

/// Compute the backward witness trace for one regression within its function.
///
/// The witness is source-first: the earliest known cause of Nullable is first,
/// and the Consumer step (the None-dereference) is last.
pub fn compute_witness(
    func: &NullFunctionFacts,
    regression: &NullRegression,
    file_facts: &NullFileFacts,
    summaries: &HashMap<String, &FuncSummary>,
) -> Vec<WitnessStep> {
    // Flatten branch arms / loop bodies into execution order so consumers
    // nested inside control flow are reachable by the linear backward trace.
    let flat = flatten(&func.facts);
    let consumer_idx = flat.iter().position(|f| {
        matches!(f, NullFact::Consumer(c) if c.node == regression.consumer_node)
    });

    let Some(consumer_idx) = consumer_idx else {
        return vec![WitnessStep::Opaque { symbol: regression.object_symbol.clone() }];
    };

    let mut steps = trace_backward(
        &flat,
        &regression.object_symbol,
        consumer_idx,
        file_facts,
        summaries,
        MAX_WITNESS_DEPTH,
    );

    steps.push(WitnessStep::Consumer {
        node: regression.consumer_node,
        symbol: regression.object_symbol.clone(),
        kind: regression.kind,
    });

    steps
}

// ── Witness internals ─────────────────────────────────────────────────────────

/// Flatten structured facts (branch arms, loop bodies) into a single
/// execution-order list of leaf facts for linear witness tracing.
fn flatten(facts: &[NullFact]) -> Vec<NullFact> {
    let mut out = Vec::new();
    flatten_into(facts, &mut out);
    out
}

fn flatten_into(facts: &[NullFact], out: &mut Vec<NullFact>) {
    for f in facts {
        match f {
            NullFact::Branch(b) => {
                flatten_into(&b.then_arm, out);
                flatten_into(&b.else_arm, out);
            }
            NullFact::Loop(l) => flatten_into(&l.body, out),
            other => out.push(other.clone()),
        }
    }
}

/// Trace backward through `facts` to explain why `symbol` is Nullable at `before_idx`.
///
/// Returns steps source-first (the Consumer step is NOT appended here — caller does it).
fn trace_backward(
    facts: &[NullFact],
    symbol: &str,
    before_idx: usize,
    file_facts: &NullFileFacts,
    summaries: &HashMap<String, &FuncSummary>,
    depth: usize,
) -> Vec<WitnessStep> {
    if depth == 0 {
        return vec![WitnessStep::Opaque { symbol: symbol.to_string() }];
    }

    // Find the last Param or Assign for `symbol` strictly before `before_idx`.
    let def = facts[..before_idx]
        .iter()
        .enumerate()
        .rev()
        .find(|(_, f)| is_def_for(f, symbol));

    match def {
        None => vec![WitnessStep::Opaque { symbol: symbol.to_string() }],

        Some((_, NullFact::Param(d))) => {
            vec![WitnessStep::NullableParam { node: d.node, symbol: symbol.to_string() }]
        }

        Some((def_idx, NullFact::Assign(d))) => match &d.rhs {
            RhsNullability::Literal(NullabilityValue::Nullable) => {
                vec![WitnessStep::NoneAssignment { node: d.node, symbol: symbol.to_string() }]
            }
            RhsNullability::FromVar(src) => {
                let src = src.clone();
                let node = d.node;
                let mut steps =
                    trace_backward(facts, &src, def_idx, file_facts, summaries, depth - 1);
                steps.push(WitnessStep::VariableCopy {
                    node,
                    from: src,
                    to: symbol.to_string(),
                });
                steps
            }
            RhsNullability::Call { callee, args } => {
                let callee = callee.clone();
                let args = args.clone();
                let node = d.node;
                let mut steps = explain_call_nullable(
                    &callee,
                    &args,
                    facts,
                    def_idx,
                    file_facts,
                    summaries,
                    depth - 1,
                );
                steps.push(WitnessStep::CallReturn { node, callee, to: symbol.to_string() });
                steps
            }
            _ => vec![WitnessStep::Opaque { symbol: symbol.to_string() }],
        },

        Some(_) => vec![WitnessStep::Opaque { symbol: symbol.to_string() }],
    }
}

fn is_def_for(fact: &NullFact, symbol: &str) -> bool {
    match fact {
        NullFact::Param(d) => d.symbol == symbol,
        NullFact::Assign(d) => d.symbol == symbol,
        _ => false,
    }
}

/// Explain why a call to `callee` with `args` returns Nullable.
///
/// Either the callee intrinsically returns Nullable (base_return), or one of the
/// Nullable args propagates through (nullable_from_param).
fn explain_call_nullable(
    callee: &str,
    args: &[CallArgKind],
    caller_facts: &[NullFact],
    call_idx: usize,
    file_facts: &NullFileFacts,
    summaries: &HashMap<String, &FuncSummary>,
    depth: usize,
) -> Vec<WitnessStep> {
    if depth == 0 {
        return vec![WitnessStep::Opaque { symbol: callee.to_string() }];
    }

    let Some(summary) = summaries.get(callee) else {
        return vec![WitnessStep::Opaque { symbol: callee.to_string() }];
    };

    // Case 1: callee intrinsically returns None regardless of args.
    if summary.base_return == NullabilityValue::Nullable {
        if let Some(callee_func) =
            file_facts.functions.iter().find(|f| f.func_name == callee)
        {
            return trace_callee_return(callee_func, file_facts, summaries, depth - 1);
        }
        return vec![WitnessStep::Opaque { symbol: callee.to_string() }];
    }

    // Case 2: a Nullable variable argument propagates through.
    for (i, arg) in args.iter().enumerate() {
        if let CallArgKind::Var(sym) = arg
            && summary.nullable_from_param.get(i).copied().unwrap_or(false)
        {
            return trace_backward(caller_facts, sym, call_idx, file_facts, summaries, depth - 1);
        }
    }

    // Case 3: a NullLiteral argument propagates through.
    for (i, arg) in args.iter().enumerate() {
        if matches!(arg, CallArgKind::NullLiteral)
            && summary.nullable_from_param.get(i).copied().unwrap_or(false)
        {
            return vec![WitnessStep::Opaque { symbol: "None".to_string() }];
        }
    }

    vec![WitnessStep::Opaque { symbol: callee.to_string() }]
}

/// Trace which return statement in `callee_func` contributes the Nullable return.
fn trace_callee_return(
    callee_func: &NullFunctionFacts,
    file_facts: &NullFileFacts,
    summaries: &HashMap<String, &FuncSummary>,
    depth: usize,
) -> Vec<WitnessStep> {
    let flat = flatten(&callee_func.facts);
    let return_facts: Vec<(usize, NullReturn)> = flat
        .iter()
        .enumerate()
        .filter_map(|(i, f)| if let NullFact::Return(r) = f { Some((i, r.clone())) } else { None })
        .collect();

    for (pos, ret) in &return_facts {
        match &ret.rhs {
            RhsNullability::Literal(NullabilityValue::Nullable) => {
                return vec![WitnessStep::NoneAssignment {
                    node: ret.node,
                    symbol: callee_func.func_name.clone(),
                }];
            }
            RhsNullability::FromVar(sym) => {
                return trace_backward(&flat, sym, *pos, file_facts, summaries, depth);
            }
            RhsNullability::Call { callee, args } => {
                return explain_call_nullable(
                    callee,
                    args,
                    &flat,
                    *pos,
                    file_facts,
                    summaries,
                    depth,
                );
            }
            _ => continue,
        }
    }

    vec![WitnessStep::Opaque { symbol: callee_func.func_name.clone() }]
}

