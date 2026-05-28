use std::collections::{HashMap, HashSet};
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
    for (func_node, regs) in &regressions {
        let func = file_facts.functions.iter().find(|f| &f.func_node == func_node);
        for reg in regs {
            let witness = func
                .map(|f| compute_witness(f, reg, &file_facts, &summary_map))
                .unwrap_or_default();
            reports.push(RegressionReport { regression: reg.clone(), witness });
        }
    }
    reports
}

// ── Pure functions ────────────────────────────────────────────────────────────

/// Diff two snapshots of regression reports.
///
/// `before` and `after` are the results before and after an edit.
/// Returns the blast radius (changed consumer nodes), newly appearing regressions,
/// and regressions that disappeared.
pub fn diff_results(before: &[RegressionReport], after: &[RegressionReport]) -> DiffResult {
    let before_nodes: HashSet<NodeId> =
        before.iter().map(|r| r.regression.consumer_node).collect();
    let after_nodes: HashSet<NodeId> = after.iter().map(|r| r.regression.consumer_node).collect();

    let mut blast_radius: Vec<NodeId> =
        before_nodes.symmetric_difference(&after_nodes).copied().collect();
    blast_radius.sort();

    let new_regressions: Vec<RegressionReport> = after
        .iter()
        .filter(|r| !before_nodes.contains(&r.regression.consumer_node))
        .cloned()
        .collect();

    let fixed_regressions: Vec<NullRegression> = before
        .iter()
        .filter(|r| !after_nodes.contains(&r.regression.consumer_node))
        .map(|r| r.regression.clone())
        .collect();

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
    let consumer_idx = func.facts.iter().position(|f| {
        matches!(f, NullFact::Consumer(c) if c.node == regression.consumer_node)
    });

    let Some(consumer_idx) = consumer_idx else {
        return vec![WitnessStep::Opaque { symbol: regression.object_symbol.clone() }];
    };

    let mut steps = trace_backward(
        &func.facts,
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
    let return_facts: Vec<(usize, &NullReturn)> = callee_func
        .facts
        .iter()
        .enumerate()
        .filter_map(|(i, f)| if let NullFact::Return(r) = f { Some((i, r)) } else { None })
        .collect();

    for (pos, ret) in return_facts {
        match &ret.rhs {
            RhsNullability::Literal(NullabilityValue::Nullable) => {
                return vec![WitnessStep::NoneAssignment {
                    node: ret.node,
                    symbol: callee_func.func_name.clone(),
                }];
            }
            RhsNullability::FromVar(sym) => {
                return trace_backward(
                    &callee_func.facts,
                    sym,
                    pos,
                    file_facts,
                    summaries,
                    depth,
                );
            }
            RhsNullability::Call { callee, args } => {
                let callee = callee.clone();
                let args = args.clone();
                return explain_call_nullable(
                    &callee,
                    &args,
                    &callee_func.facts,
                    pos,
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

