use std::collections::HashMap;
use wake_engine::{Db, SourceFile};
use wake_schema::{
    CallArgKind, NodeId, NullCallSite, NullFact, NullFunctionFacts, NullRegression,
    NullabilityValue, RhsNullability,
};

pub type FunctionRegressions = Vec<(NodeId, Vec<NullRegression>)>;

// ── Summary types ─────────────────────────────────────────────────────────────

/// Interprocedural summary for one function.
///
/// Computed by running the forward dataflow under two kinds of entry environments:
///   1. All params = Unknown → base_return
///   2. Each param i = Nullable (others Unknown) → nullable_from_param[i], regressions_from_param[i]
///
/// At a call site `result = f(a0, a1, ..., ak)`:
///   - ret = base_return
///   - For each i where arg_null[i] == Nullable and nullable_from_param[i]: ret = Nullable
///   - Emit regressions_from_param[i] for each such i
#[derive(Clone, Debug, PartialEq, Eq, salsa::Update)]
pub struct FuncSummary {
    pub param_names: Vec<String>,
    /// Return nullability when all params are Unknown.
    pub base_return: NullabilityValue,
    /// nullable_from_param[i]: param i being Nullable → return is Nullable.
    pub nullable_from_param: Vec<bool>,
    /// regressions_from_param[i]: regressions inside the function when param i is Nullable.
    pub regressions_from_param: Vec<Vec<NullRegression>>,
}

/// All summaries for one file, in function-order (same as NullFileFacts.functions).
/// Stored as a Vec to satisfy salsa's Update requirement.
#[derive(Clone, Debug, PartialEq, Eq, Default, salsa::Update)]
pub struct FileSummaries {
    pub entries: Vec<(String, FuncSummary)>,
}

impl FileSummaries {
    pub fn get(&self, name: &str) -> Option<&FuncSummary> {
        self.entries.iter().find(|(n, _)| n == name).map(|(_, s)| s)
    }
}

// ── Salsa tracked functions ───────────────────────────────────────────────────

/// Compute interprocedural nullability summaries for all functions in `file`.
///
/// Memoized per file: changing file B does not cause file A's summaries to recompute.
/// Functions are processed in declaration order; each function's summary is available
/// to later functions in the same file (handles non-recursive call chains).
#[salsa::tracked]
pub fn null_summaries(db: &dyn Db, file: SourceFile) -> FileSummaries {
    let file_facts = wake_extract_py::extract_null_file(db, file);
    compute_file_summaries(&file_facts)
}

/// Compute intraprocedural + interprocedural nullability regressions for `file`.
///
/// Uses summaries from `null_summaries` to propagate None across call boundaries.
#[salsa::tracked]
pub fn null_regressions(db: &dyn Db, file: SourceFile) -> FunctionRegressions {
    let file_facts = wake_extract_py::extract_null_file(db, file);
    let file_summaries = null_summaries(db, file);

    // Build summary map for quick lookup.
    let summaries: HashMap<String, &FuncSummary> =
        file_summaries.entries.iter().map(|(n, s)| (n.clone(), s)).collect();

    file_facts
        .functions
        .iter()
        .map(|f| {
            let param_names = collect_param_names(f);
            let entry_env: HashMap<String, NullabilityValue> =
                param_names.iter().map(|p| (p.clone(), NullabilityValue::Unknown)).collect();
            let (regs, _) = analyze_function_full(f, entry_env, &summaries);
            // Include interprocedural regressions triggered by callee analysis.
            (f.func_node, regs)
        })
        .collect()
}

// ── Summary computation ───────────────────────────────────────────────────────

fn compute_file_summaries(file_facts: &wake_schema::NullFileFacts) -> FileSummaries {
    // Map of already-computed summaries (by function name) — built incrementally
    // as we process functions in declaration order.
    let mut computed: HashMap<String, FuncSummary> = HashMap::new();
    let mut entries: Vec<(String, FuncSummary)> = Vec::new();

    for func in &file_facts.functions {
        let param_names = collect_param_names(func);
        let summary = compute_func_summary(func, &param_names, &computed);
        computed.insert(func.func_name.clone(), summary.clone());
        entries.push((func.func_name.clone(), summary));
    }

    FileSummaries { entries }
}

fn compute_func_summary(
    func: &NullFunctionFacts,
    param_names: &[String],
    summaries: &HashMap<String, FuncSummary>,
) -> FuncSummary {
    // Borrow summaries by reference for lookup.
    let summary_refs: HashMap<String, &FuncSummary> =
        summaries.iter().map(|(k, v)| (k.clone(), v)).collect();

    // Step 1: run with all params Unknown → base return.
    let base_env = uniform_env(param_names, NullabilityValue::Unknown);
    let (_, base_return) = analyze_function_full(func, base_env, &summary_refs);

    let n = param_names.len();
    let mut nullable_from_param = vec![false; n];
    let mut regressions_from_param = vec![Vec::new(); n];

    // Step 2: for each param, run with that param Nullable (others Unknown).
    for i in 0..n {
        let mut env = uniform_env(param_names, NullabilityValue::Unknown);
        env.insert(param_names[i].clone(), NullabilityValue::Nullable);
        let (regs, ret) = analyze_function_full(func, env, &summary_refs);
        nullable_from_param[i] = ret == NullabilityValue::Nullable;
        regressions_from_param[i] = regs;
    }

    FuncSummary {
        param_names: param_names.to_vec(),
        base_return,
        nullable_from_param,
        regressions_from_param,
    }
}

fn uniform_env(param_names: &[String], val: NullabilityValue) -> HashMap<String, NullabilityValue> {
    param_names.iter().map(|p| (p.clone(), val)).collect()
}

// ── Forward dataflow ──────────────────────────────────────────────────────────

/// Run the forward dataflow for `func` starting from `initial_env`.
///
/// Returns `(regressions, return_nullability)`.
///
/// The return value tracks what value flows out of the function:
/// - `NullFact::Return(r)` contributes to the accumulated return nullability.
/// - If no Return fact is seen, returns Unknown (function may return None implicitly,
///   but we decline to assert — precision-over-soundness).
/// - Multiple return facts are joined with the three-valued lattice join.
pub fn analyze_function_full(
    func: &NullFunctionFacts,
    initial_env: HashMap<String, NullabilityValue>,
    summaries: &HashMap<String, &FuncSummary>,
) -> (Vec<NullRegression>, NullabilityValue) {
    let mut env = initial_env;
    let mut regressions: Vec<NullRegression> = Vec::new();
    let mut return_val: Option<NullabilityValue> = None;

    for fact in &func.facts {
        match fact {
            NullFact::Param(def) => {
                // Params seed the env from the caller-supplied initial_env,
                // but annotation overrides if it's not Unknown.
                if def.annotation != NullabilityValue::Unknown {
                    env.insert(def.symbol.clone(), def.annotation);
                }
                // else: keep what the caller provided (already in initial_env).
            }
            NullFact::Assign(def) => {
                let value = if def.annotation != NullabilityValue::Unknown {
                    def.annotation
                } else {
                    eval_rhs(&def.rhs, &env, summaries, &mut regressions, func.func_node)
                };
                env.insert(def.symbol.clone(), value);
            }
            NullFact::Consumer(consumer) => {
                if env.get(&consumer.object_symbol) == Some(&NullabilityValue::Nullable) {
                    regressions.push(NullRegression {
                        func_node: func.func_node,
                        consumer_node: consumer.node,
                        object_symbol: consumer.object_symbol.clone(),
                        kind: consumer.kind,
                    });
                }
            }
            NullFact::CallStmt(call) => {
                // Bare call: collect callee-internal regressions for Nullable args.
                handle_call_stmt(call, &env, summaries, &mut regressions);
            }
            NullFact::Return(ret) => {
                let v = eval_rhs(&ret.rhs, &env, summaries, &mut regressions, func.func_node);
                return_val = Some(match return_val {
                    None => v,
                    Some(prev) => prev.join(v),
                });
            }
            NullFact::Unknown(_) => {
                env.clear();
                // Return accumulation is not cleared: control flow inside the opaque
                // region may have contributed returns, but we've lost track of them.
                // We conservatively keep whatever we had before the barrier.
            }
        }
    }

    let return_null = return_val.unwrap_or(NullabilityValue::Unknown);
    (regressions, return_null)
}

/// Public wrapper used by Phase 2 tests (no summaries).
pub fn analyze_function(func: &NullFunctionFacts) -> Vec<NullRegression> {
    let param_names = collect_param_names(func);
    let entry_env = uniform_env(&param_names, NullabilityValue::Unknown);
    let empty: HashMap<String, &FuncSummary> = HashMap::new();
    let (regs, _) = analyze_function_full(func, entry_env, &empty);
    regs
}

// ── Call site handling ────────────────────────────────────────────────────────

/// Apply a summary at a call site, returning the return value's nullability.
fn apply_summary(
    summary: &FuncSummary,
    arg_nulls: &[NullabilityValue],
    regressions: &mut Vec<NullRegression>,
) -> NullabilityValue {
    let mut ret = summary.base_return;
    for (i, &arg_null) in arg_nulls.iter().enumerate() {
        if arg_null == NullabilityValue::Nullable {
            if i < summary.nullable_from_param.len() && summary.nullable_from_param[i] {
                ret = NullabilityValue::Nullable;
            }
            if i < summary.regressions_from_param.len() {
                regressions.extend(summary.regressions_from_param[i].iter().cloned());
            }
        }
    }
    ret
}

fn resolve_args(
    args: &[CallArgKind],
    env: &HashMap<String, NullabilityValue>,
) -> Vec<NullabilityValue> {
    args.iter()
        .map(|a| match a {
            CallArgKind::Var(s) => {
                env.get(s.as_str()).copied().unwrap_or(NullabilityValue::Unknown)
            }
            CallArgKind::NullLiteral => NullabilityValue::Nullable,
            CallArgKind::NonNullLiteral => NullabilityValue::NonNull,
            CallArgKind::Unknown => NullabilityValue::Unknown,
        })
        .collect()
}

fn handle_call_stmt(
    call: &NullCallSite,
    env: &HashMap<String, NullabilityValue>,
    summaries: &HashMap<String, &FuncSummary>,
    regressions: &mut Vec<NullRegression>,
) {
    if let Some(summary) = summaries.get(call.callee.as_str()) {
        let arg_nulls = resolve_args(&call.args, env);
        apply_summary(summary, &arg_nulls, regressions);
    }
    // Unknown callee: no regressions propagated (precision-over-soundness).
}

fn eval_rhs(
    rhs: &RhsNullability,
    env: &HashMap<String, NullabilityValue>,
    summaries: &HashMap<String, &FuncSummary>,
    regressions: &mut Vec<NullRegression>,
    _func_node: NodeId,
) -> NullabilityValue {
    match rhs {
        RhsNullability::Literal(v) => *v,
        RhsNullability::FromVar(sym) => {
            env.get(sym.as_str()).copied().unwrap_or(NullabilityValue::Unknown)
        }
        RhsNullability::Call { callee, args } => {
            if let Some(summary) = summaries.get(callee.as_str()) {
                let arg_nulls = resolve_args(args, env);
                apply_summary(summary, &arg_nulls, regressions)
            } else {
                // Callee not in file (stdlib, external): Unknown.
                NullabilityValue::Unknown
            }
        }
        RhsNullability::Unknown => NullabilityValue::Unknown,
    }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

/// Extract parameter names from the ordered fact list (they appear first).
pub fn collect_param_names(func: &NullFunctionFacts) -> Vec<String> {
    func.facts
        .iter()
        .take_while(|f| matches!(f, NullFact::Param(_)))
        .filter_map(|f| if let NullFact::Param(d) = f { Some(d.symbol.clone()) } else { None })
        .collect()
}
