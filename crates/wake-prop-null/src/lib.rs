use std::collections::{HashMap, HashSet};
use wake_engine::{Db, SourceFile, Workspace};
use wake_schema::{
    BranchCondition, CallArgKind, NarrowEffect, NodeId, NullBranch, NullCallSite, NullFact,
    NullFunctionFacts, NullLoop, NullRegression, NullabilityValue, RhsNullability,
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

    // Analyze every function as a potential entry point (params Unknown). Each
    // regression carries its own `func_node` (the function *containing* the
    // consumer), so interprocedural regressions surfaced at a call site are
    // attributed to the callee, not the caller.
    let mut all: Vec<NullRegression> = Vec::new();
    for f in &file_facts.functions {
        let param_names = collect_param_names(f);
        let entry_env: HashMap<String, NullabilityValue> =
            param_names.iter().map(|p| (p.clone(), NullabilityValue::Unknown)).collect();
        let (regs, _) = analyze_function_full(f, "", entry_env, &summaries);
        all.extend(regs);
    }

    // Dedup: a callee deref reached via several call sites is one regression.
    let mut seen: HashSet<NullRegression> = HashSet::new();
    all.retain(|r| seen.insert(r.clone()));

    // Group by the function that contains the consumer, in declaration order.
    let mut by_func: HashMap<NodeId, Vec<NullRegression>> = HashMap::new();
    for r in all {
        by_func.entry(r.func_node).or_default().push(r);
    }
    file_facts
        .functions
        .iter()
        .map(|f| (f.func_node, by_func.remove(&f.func_node).unwrap_or_default()))
        .collect()
}

// ── Workspace (cross-file) analysis ────────────────────────────────────────────

/// Global interprocedural summaries across all files in the workspace.
///
/// A function name defined exactly once across the workspace is resolvable by
/// any caller (in any file); an ambiguous name resolves to Unknown. This is how
/// cross-file value flow is achieved without a full Python import resolver
/// (`from m import f; f()` and `import m; m.f()` both resolve by unique name).
#[salsa::tracked]
pub fn workspace_summaries(db: &dyn Db, ws: Workspace) -> FileSummaries {
    let files = ws.files(db);
    let facts: Vec<(String, wake_schema::NullFileFacts)> = files
        .iter()
        .map(|(path, sf)| (path.clone(), wake_extract_py::extract_null_file(db, *sf)))
        .collect();
    let funcs: Vec<(&str, &NullFunctionFacts)> = facts
        .iter()
        .flat_map(|(path, ff)| ff.functions.iter().map(move |f| (path.as_str(), f)))
        .collect();
    FileSummaries { entries: compute_workspace_summaries(&funcs) }
}

/// Cross-file nullability regressions for every function in the workspace.
/// Each regression carries the path of the file containing its consumer.
#[salsa::tracked]
pub fn workspace_regressions(db: &dyn Db, ws: Workspace) -> Vec<NullRegression> {
    let files = ws.files(db);
    let sums = workspace_summaries(db, ws);
    let summaries: HashMap<String, &FuncSummary> =
        sums.entries.iter().map(|(n, s)| (n.clone(), s)).collect();

    let mut all: Vec<NullRegression> = Vec::new();
    for (path, sf) in files {
        let ff = wake_extract_py::extract_null_file(db, *sf);
        for f in &ff.functions {
            let param_names = collect_param_names(f);
            let entry_env: HashMap<String, NullabilityValue> =
                param_names.iter().map(|p| (p.clone(), NullabilityValue::Unknown)).collect();
            let (regs, _) = analyze_function_full(f, path, entry_env, &summaries);
            all.extend(regs);
        }
    }
    let mut seen: HashSet<NullRegression> = HashSet::new();
    all.retain(|r| seen.insert(r.clone()));
    all
}

/// Fixpoint summary computation over all (file, function) pairs in the workspace,
/// resolving only uniquely-named functions.
fn compute_workspace_summaries(funcs: &[(&str, &NullFunctionFacts)]) -> Vec<(String, FuncSummary)> {
    let mut counts: HashMap<&str, usize> = HashMap::new();
    for (_p, f) in funcs {
        *counts.entry(f.func_name.as_str()).or_default() += 1;
    }
    let unique: Vec<(&str, &NullFunctionFacts)> =
        funcs.iter().copied().filter(|(_p, f)| counts[f.func_name.as_str()] == 1).collect();

    let mut computed: HashMap<String, FuncSummary> = HashMap::new();
    let bound = unique.len() + 1;
    for _ in 0..bound {
        let mut changed = false;
        for &(path, f) in &unique {
            let param_names = collect_param_names(f);
            let summary = compute_func_summary(f, path, &param_names, &computed);
            if computed.get(&f.func_name) != Some(&summary) {
                computed.insert(f.func_name.clone(), summary);
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }

    unique
        .iter()
        .filter_map(|&(_p, f)| computed.get(&f.func_name).map(|s| (f.func_name.clone(), s.clone())))
        .collect()
}

// ── Summary computation ───────────────────────────────────────────────────────

fn compute_file_summaries(file_facts: &wake_schema::NullFileFacts) -> FileSummaries {
    let mut computed: HashMap<String, FuncSummary> = HashMap::new();

    // Iterate to a fixpoint so a function whose summary depends on a *later*
    // declared callee still resolves — results are independent of declaration
    // order. Bounded by the function count so recursive call graphs terminate
    // (they converge to a safe approximation at the bound).
    let bound = file_facts.functions.len() + 1;
    for _ in 0..bound {
        let mut changed = false;
        for func in &file_facts.functions {
            let param_names = collect_param_names(func);
            let summary = compute_func_summary(func, "", &param_names, &computed);
            if computed.get(&func.func_name) != Some(&summary) {
                computed.insert(func.func_name.clone(), summary);
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }

    // Emit entries in declaration order.
    let entries = file_facts
        .functions
        .iter()
        .filter_map(|f| computed.get(&f.func_name).map(|s| (f.func_name.clone(), s.clone())))
        .collect();
    FileSummaries { entries }
}

fn compute_func_summary(
    func: &NullFunctionFacts,
    file: &str,
    param_names: &[String],
    summaries: &HashMap<String, FuncSummary>,
) -> FuncSummary {
    // Borrow summaries by reference for lookup.
    let summary_refs: HashMap<String, &FuncSummary> =
        summaries.iter().map(|(k, v)| (k.clone(), v)).collect();

    // Step 1: run with all params Unknown → base return.
    let base_env = uniform_env(param_names, NullabilityValue::Unknown);
    let (_, base_return) = analyze_function_full(func, file, base_env, &summary_refs);

    let n = param_names.len();
    let mut nullable_from_param = vec![false; n];
    let mut regressions_from_param = vec![Vec::new(); n];

    // Step 2: for each param, run with that param Nullable (others Unknown).
    for i in 0..n {
        let mut env = uniform_env(param_names, NullabilityValue::Unknown);
        env.insert(param_names[i].clone(), NullabilityValue::Nullable);
        let (regs, ret) = analyze_function_full(func, file, env, &summary_refs);
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
    file: &str,
    initial_env: HashMap<String, NullabilityValue>,
    summaries: &HashMap<String, &FuncSummary>,
) -> (Vec<NullRegression>, NullabilityValue) {
    let ctx = AnalysisCtx {
        file,
        func_node: func.func_node,
        func_name: &func.func_name,
        summaries,
    };
    let mut env = initial_env;
    let mut regressions: Vec<NullRegression> = Vec::new();
    let mut return_val: Option<NullabilityValue> = None;
    run_facts(&func.facts, &mut env, &ctx, &mut regressions, &mut return_val);
    (regressions, return_val.unwrap_or(NullabilityValue::Unknown))
}

/// Per-function context threaded through the recursive walk.
struct AnalysisCtx<'a> {
    /// Path of the file being analyzed (empty for single-file, path-less analysis).
    file: &'a str,
    func_node: NodeId,
    func_name: &'a str,
    summaries: &'a HashMap<String, &'a FuncSummary>,
}

type Env = HashMap<String, NullabilityValue>;

/// Forward-analyze a sequence of facts, mutating `env`, collecting regressions,
/// and accumulating the function's return nullability.
fn run_facts(
    facts: &[NullFact],
    env: &mut Env,
    ctx: &AnalysisCtx,
    regressions: &mut Vec<NullRegression>,
    return_val: &mut Option<NullabilityValue>,
) {
    for fact in facts {
        match fact {
            NullFact::Param(def) => {
                // Params seed from the caller-supplied env; a non-Unknown
                // annotation overrides it.
                if def.annotation != NullabilityValue::Unknown {
                    env.insert(def.symbol.clone(), def.annotation);
                }
            }
            NullFact::Assign(def) => {
                let value = if def.annotation != NullabilityValue::Unknown {
                    def.annotation
                } else {
                    eval_rhs(&def.rhs, env, ctx.summaries, regressions)
                };
                env.insert(def.symbol.clone(), value);
            }
            NullFact::Consumer(consumer) => {
                if env.get(&consumer.object_symbol) == Some(&NullabilityValue::Nullable) {
                    regressions.push(NullRegression {
                        file: ctx.file.to_string(),
                        func_node: ctx.func_node,
                        func_name: ctx.func_name.to_string(),
                        consumer_node: consumer.node,
                        object_symbol: consumer.object_symbol.clone(),
                        kind: consumer.kind,
                    });
                }
            }
            NullFact::CallStmt(call) => {
                handle_call_stmt(call, env, ctx.summaries, regressions);
            }
            NullFact::Return(ret) => {
                let v = eval_rhs(&ret.rhs, env, ctx.summaries, regressions);
                *return_val = Some(match *return_val {
                    None => v,
                    Some(prev) => prev.join(v),
                });
            }
            NullFact::Branch(br) => run_branch(br, env, ctx, regressions, return_val),
            NullFact::Loop(lp) => run_loop(lp, env, ctx, regressions, return_val),
            NullFact::Assume(cond) => apply_narrowing(env, cond, true),
            NullFact::Unknown(_) => {
                // Unparseable / not-yet-modeled region: we lose all certainty.
                // Clearing to "no positive evidence" is precision-safe (Unknown
                // never triggers a report).
                env.clear();
            }
        }
    }
}

/// Analyze both arms of a branch from narrowed copies of `env`, then merge the
/// arm-exit environments with the precision-preserving lattice join.
fn run_branch(
    br: &NullBranch,
    env: &mut Env,
    ctx: &AnalysisCtx,
    regressions: &mut Vec<NullRegression>,
    return_val: &mut Option<NullabilityValue>,
) {
    let mut then_env = env.clone();
    apply_narrowing(&mut then_env, &br.condition, true);
    run_facts(&br.then_arm, &mut then_env, ctx, regressions, return_val);

    // The false side covers an explicit `else` *or* the implicit fall-through.
    let mut else_env = env.clone();
    apply_narrowing(&mut else_env, &br.condition, false);
    run_facts(&br.else_arm, &mut else_env, ctx, regressions, return_val);

    *env = join_envs(&then_env, &else_env);
}

/// Analyze a loop body once from the entry environment (which captures the real
/// first-iteration behavior), then merge with the pre-loop env since the body
/// may run zero times.
fn run_loop(
    lp: &NullLoop,
    env: &mut Env,
    ctx: &AnalysisCtx,
    regressions: &mut Vec<NullRegression>,
    return_val: &mut Option<NullabilityValue>,
) {
    let mut body_env = env.clone();
    // Loop-bound names (e.g. the `for` target) have unknown nullability.
    for name in &lp.bound {
        body_env.insert(name.clone(), NullabilityValue::Unknown);
    }
    // `while` condition narrowing applies to the body (truthy side).
    apply_narrowing(&mut body_env, &lp.condition, true);
    run_facts(&lp.body, &mut body_env, ctx, regressions, return_val);

    *env = join_envs(env, &body_env);
}

/// Apply a condition's narrowing effect to one side of a branch/loop.
fn apply_narrowing(env: &mut Env, cond: &BranchCondition, true_side: bool) {
    // A condition we can't interpret tells us nothing definite about the
    // variables it mentions — clear them so an unmodeled guard never produces
    // a false positive.
    for r in &cond.opaque_refs {
        env.insert(r.clone(), NullabilityValue::Unknown);
    }
    if let Some(sym) = &cond.symbol {
        let effect = if true_side { cond.on_true } else { cond.on_false };
        match effect {
            NarrowEffect::Keep => {}
            NarrowEffect::NonNull => {
                env.insert(sym.clone(), NullabilityValue::NonNull);
            }
            NarrowEffect::Nullable => {
                env.insert(sym.clone(), NullabilityValue::Nullable);
            }
            NarrowEffect::Unknown => {
                env.insert(sym.clone(), NullabilityValue::Unknown);
            }
        }
    }
}

/// Merge two environments at a control-flow join point. A symbol defined on only
/// one path, or with disagreeing values, becomes Unknown (precision over
/// soundness: we decline to assert rather than risk a false report).
fn join_envs(a: &Env, b: &Env) -> Env {
    let keys: HashSet<&String> = a.keys().chain(b.keys()).collect();
    keys.into_iter()
        .map(|k| {
            let v = match (a.get(k), b.get(k)) {
                (Some(&x), Some(&y)) => x.join(y),
                _ => NullabilityValue::Unknown,
            };
            (k.clone(), v)
        })
        .collect()
}

/// Public wrapper used by Phase 2 tests (no summaries).
pub fn analyze_function(func: &NullFunctionFacts) -> Vec<NullRegression> {
    let param_names = collect_param_names(func);
    let entry_env = uniform_env(&param_names, NullabilityValue::Unknown);
    let empty: HashMap<String, &FuncSummary> = HashMap::new();
    let (regs, _) = analyze_function_full(func, "", entry_env, &empty);
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
) -> NullabilityValue {
    match rhs {
        RhsNullability::Literal(v) => *v,
        RhsNullability::FromVar(sym) => {
            env.get(sym.as_str()).copied().unwrap_or(NullabilityValue::Unknown)
        }
        RhsNullability::Call { callee, args, receiver } => {
            if receiver_blocks_resolution(receiver, env) {
                return NullabilityValue::Unknown;
            }
            if let Some(summary) = summaries.get(callee.as_str()) {
                let arg_nulls = resolve_args(args, env);
                apply_summary(summary, &arg_nulls, regressions)
            } else {
                // Callee not in scope (stdlib, external, ambiguous name): Unknown.
                NullabilityValue::Unknown
            }
        }
        RhsNullability::Unknown => NullabilityValue::Unknown,
    }
}

/// Returns true when the receiver is a known local/param (env-gated), meaning the call is a
/// method call on a local object and should NOT be resolved as a cross-file module call.
fn receiver_blocks_resolution(receiver: &Option<String>, env: &HashMap<String, NullabilityValue>) -> bool {
    match receiver {
        None => false,
        Some(name) => env.contains_key(name.as_str()),
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
