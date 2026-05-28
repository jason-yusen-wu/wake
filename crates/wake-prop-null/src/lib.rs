use std::collections::HashMap;
use wake_engine::{Db, SourceFile};
use wake_schema::{
    NodeId, NullFact, NullFunctionFacts, NullRegression, NullabilityValue, RhsNullability,
};

pub type FunctionRegressions = Vec<(NodeId, Vec<NullRegression>)>;

/// Compute intraprocedural nullability regressions for all functions in `file`.
///
/// A regression is a consumer site (x.attr, x[i], x()) where x is provably
/// Nullable at that point in the forward dataflow.  We use the three-valued
/// lattice (NonNull | Nullable | Unknown) with precision-over-soundness join:
/// disagreement at merge points yields Unknown, not Nullable, so we never
/// report a false positive across a control-flow barrier.
#[salsa::tracked]
pub fn null_regressions(db: &dyn Db, file: SourceFile) -> FunctionRegressions {
    let file_facts = wake_extract_py::extract_null_file(db, file);
    file_facts
        .functions
        .iter()
        .map(|f| (f.func_node, analyze_function(f)))
        .collect()
}

/// Forward dataflow over the ordered NullFact list for one function.
///
/// env maps each symbol to its current NullabilityValue.  The algorithm:
///   Param  → seed env from annotation (Unknown if absent)
///   Assign → prefer explicit annotation; fall back to eval_rhs(rhs, env)
///   Consumer → if env[symbol] == Nullable, emit NullRegression
///   Unknown  → env.clear() (control-flow barrier: all certainty lost)
pub fn analyze_function(func: &NullFunctionFacts) -> Vec<NullRegression> {
    let mut env: HashMap<String, NullabilityValue> = HashMap::new();
    let mut regressions: Vec<NullRegression> = Vec::new();

    for fact in &func.facts {
        match fact {
            NullFact::Param(def) => {
                env.insert(def.symbol.clone(), def.annotation);
            }
            NullFact::Assign(def) => {
                let value = if def.annotation != NullabilityValue::Unknown {
                    def.annotation
                } else {
                    eval_rhs(&def.rhs, &env)
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
            NullFact::Unknown(_) => {
                env.clear();
            }
        }
    }

    regressions
}

fn eval_rhs(rhs: &RhsNullability, env: &HashMap<String, NullabilityValue>) -> NullabilityValue {
    match rhs {
        RhsNullability::Literal(v) => *v,
        RhsNullability::FromVar(sym) => {
            env.get(sym.as_str()).copied().unwrap_or(NullabilityValue::Unknown)
        }
        RhsNullability::Unknown => NullabilityValue::Unknown,
    }
}
