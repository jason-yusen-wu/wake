use std::collections::HashMap;
use wake_engine::{Db, SourceFile};
use wake_schema::{Confidence, Def, DefUseEdge, Fact, FunctionFacts, NodeId, Use};

/// Def-use edges computed for every function in a file.
pub type FunctionEdges = Vec<(NodeId, Vec<DefUseEdge>)>;

/// Compute intraprocedural def-use edges for all functions in `file`.
///
/// This is a salsa tracked function: its result is memoized and recomputed only
/// when `extract_file(file)` returns a different `FileFacts`.  Because extraction
/// is itself memoized per file, editing an unrelated file causes zero recomputation here.
#[salsa::tracked]
pub fn def_use_edges(db: &dyn Db, file: SourceFile) -> Vec<(NodeId, Vec<DefUseEdge>)> {
    let file_facts = wake_extract_py::extract_file(db, file);
    file_facts
        .functions
        .iter()
        .map(|f| (f.func_node, reaching_defs(f)))
        .collect()
}

/// Simple reaching-defs over the ordered fact list.
///
/// Facts are emitted in execution order by the extractor (RHS uses before LHS defs
/// for assignments).  Walking them in order and maintaining a "last writer" map
/// gives correct reaching-defs for straight-line code.
///
/// When a `Unknown` fact is encountered the reaching set is cleared for all symbols
/// that *might* be written inside the opaque region.  For Phase 1 we conservatively
/// clear the whole map — Phase 2 will compute a proper join at branch merge points.
fn reaching_defs(func: &FunctionFacts) -> Vec<DefUseEdge> {
    // symbol → (def NodeId, confidence)
    let mut reaching: HashMap<&str, (NodeId, Confidence)> = HashMap::new();
    let mut edges: Vec<DefUseEdge> = Vec::new();
    let mut after_unknown = false;

    for fact in &func.facts {
        match fact {
            Fact::Def(Def { node, symbol, .. }) => {
                let confidence = if after_unknown {
                    Confidence::Unknown
                } else {
                    Confidence::Definite
                };
                reaching.insert(symbol.as_str(), (*node, confidence));
            }
            Fact::Use(Use { node, symbol }) => {
                if let Some(&(def_node, confidence)) = reaching.get(symbol.as_str()) {
                    edges.push(DefUseEdge { def_node, use_node: *node, confidence });
                }
                // No reaching def: variable from outer scope or not yet defined.
                // We emit no edge rather than a false Unknown edge.
            }
            Fact::Unknown(_) => {
                // Conservative: any def that might happen in the opaque region
                // could kill or shadow what we know.  Clear the whole reaching set.
                // Phase 2 will refine this to per-symbol kills.
                reaching.clear();
                after_unknown = true;
            }
        }
    }

    edges
}
