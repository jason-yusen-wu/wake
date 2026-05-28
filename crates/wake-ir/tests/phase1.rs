use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_engine::{Db, SourceFile};
use wake_ir::def_use_edges;
use wake_schema::{Confidence, DefKind, Fact, NodeId};

// ── Shared test database ─────────────────────────────────────────────────────

#[salsa::db]
struct TestDb {
    storage: salsa::Storage<Self>,
}

#[salsa::db]
impl salsa::Database for TestDb {}

#[salsa::db]
impl Db for TestDb {}

impl Default for TestDb {
    fn default() -> Self {
        Self { storage: salsa::Storage::default() }
    }
}

fn tracking_db() -> (TestDb, Arc<Mutex<usize>>) {
    let count = Arc::new(Mutex::new(0usize));
    let cb = count.clone();
    let storage = salsa::Storage::new(Some(Box::new(move |event: salsa::Event| {
        if matches!(event.kind, salsa::EventKind::WillExecute { .. }) {
            *cb.lock().unwrap() += 1;
        }
    })));
    (TestDb { storage }, count)
}

// ── Helper: extract FileFacts for a source string ────────────────────────────

fn file_for(db: &TestDb, src: &str) -> SourceFile {
    SourceFile::new(db, src.to_string())
}

// ── Correctness: simple straight-line function ────────────────────────────────

/// def foo(x, y):
///     z = x + y
///     return z
///
/// Expected def-use edges:
///   x_param → x_use (in x + y)
///   y_param → y_use (in x + y)
///   z_def   → z_use (in return z)
#[test]
fn straight_line_def_use() {
    let db = TestDb::default();
    let src = "def foo(x, y):\n    z = x + y\n    return z\n";
    let file = file_for(&db, src);

    let all_edges = def_use_edges(&db, file);
    assert_eq!(all_edges.len(), 1, "one function");
    let (_func, edges) = &all_edges[0];

    // All edges should be Definite for straight-line code.
    assert!(
        edges.iter().all(|e| e.confidence == Confidence::Definite),
        "all edges in straight-line code are Definite"
    );

    // We should have exactly 3 edges: x→x, y→y, z→z.
    assert_eq!(edges.len(), 3, "expected 3 def-use edges, got: {edges:?}");

    // Verify using the extracted facts to find the NodeIds.
    let file_facts = wake_extract_py::extract_file(&db, file);
    let func_facts = &file_facts.functions[0];

    let param_x = func_facts.facts.iter().find_map(|f| {
        if let Fact::Def(d) = f {
            if d.symbol == "x" && d.kind == DefKind::Parameter { Some(d.node) } else { None }
        } else {
            None
        }
    });
    let param_y = func_facts.facts.iter().find_map(|f| {
        if let Fact::Def(d) = f {
            if d.symbol == "y" && d.kind == DefKind::Parameter { Some(d.node) } else { None }
        } else {
            None
        }
    });
    let def_z = func_facts.facts.iter().find_map(|f| {
        if let Fact::Def(d) = f {
            if d.symbol == "z" && d.kind == DefKind::Assign { Some(d.node) } else { None }
        } else {
            None
        }
    });

    assert!(param_x.is_some(), "x parameter not found");
    assert!(param_y.is_some(), "y parameter not found");
    assert!(def_z.is_some(), "z assignment not found");

    let has_edge = |def: NodeId, sym: &str| {
        edges.iter().any(|e| {
            e.def_node == def
                && func_facts.facts.iter().any(|f| {
                    matches!(f, Fact::Use(u) if u.node == e.use_node && u.symbol == sym)
                })
        })
    };

    assert!(has_edge(param_x.unwrap(), "x"), "x param → x use");
    assert!(has_edge(param_y.unwrap(), "y"), "y param → y use");
    assert!(has_edge(def_z.unwrap(), "z"), "z def → z use");
}

// ── Correctness: no spurious edges for parameters never used ─────────────────

#[test]
fn unused_parameter_has_no_use_edge() {
    let db = TestDb::default();
    let src = "def foo(x, y):\n    return x\n";
    let file = file_for(&db, src);

    let all_edges = def_use_edges(&db, file);
    let (_func, edges) = &all_edges[0];

    // x has an edge, y does not.
    let file_facts = wake_extract_py::extract_file(&db, file);
    let func_facts = &file_facts.functions[0];

    let param_y = func_facts.facts.iter().find_map(|f| {
        if let Fact::Def(d) = f {
            if d.symbol == "y" && d.kind == DefKind::Parameter { Some(d.node) } else { None }
        } else {
            None
        }
    });

    let y_has_edge = edges.iter().any(|e| Some(e.def_node) == param_y);
    assert!(!y_has_edge, "unused parameter y must not have a def-use edge");
}

// ── Correctness: Unknown barrier clears reaching set ─────────────────────────

/// def foo(x):
///     if x:       ← Unknown barrier
///         x = 0
///     return x    ← use of x; no reaching def (cleared by barrier)
#[test]
fn unknown_barrier_clears_reaching_set() {
    let db = TestDb::default();
    let src = "def foo(x):\n    if x:\n        x = 0\n    return x\n";
    let file = file_for(&db, src);

    let all_edges = def_use_edges(&db, file);
    let (_func, edges) = &all_edges[0];

    // After the if_statement Unknown barrier, the reaching set is cleared.
    // The `return x` use has no reaching def, so no edge is emitted.
    // (This is conservative / safe-to-be-silent.)
    let file_facts = wake_extract_py::extract_file(&db, file);
    let func_facts = &file_facts.functions[0];

    let _return_x_use = func_facts.facts.iter().find_map(|f| {
        if let Fact::Use(u) = f { if u.symbol == "x" { Some(u.node) } else { None } } else { None }
    });

    // There may be a use of `x` as the condition of the if — that one has a
    // reaching def (the parameter).  The use in `return x` after the barrier
    // should NOT have an edge.
    //
    // We verify: the last Use of `x` in the fact list (the return) has no edge.
    let last_x_use = func_facts
        .facts
        .iter()
        .filter_map(|f| if let Fact::Use(u) = f { if u.symbol == "x" { Some(u.node) } else { None } } else { None })
        .last();

    if let Some(last) = last_x_use {
        let has_edge_for_last = edges.iter().any(|e| e.use_node == last);
        assert!(
            !has_edge_for_last,
            "use of x after Unknown barrier must have no reaching-def edge"
        );
    }
}

// ── Correctness: error node in function body becomes Unknown ─────────────────

#[test]
fn parse_error_in_body_becomes_unknown() {
    let db = TestDb::default();
    // Intentionally broken: missing `=` in assignment
    let src = "def foo(x):\n    z x\n    return x\n";
    let file = file_for(&db, src);

    let file_facts = wake_extract_py::extract_file(&db, file);
    let func_facts = &file_facts.functions[0];

    let has_unknown =
        func_facts.facts.iter().any(|f| matches!(f, Fact::Unknown(_)));
    assert!(has_unknown, "broken statement should produce an Unknown fact");

    // The query must still complete — no panic.
    let all_edges = def_use_edges(&db, file);
    assert_eq!(all_edges.len(), 1);
}

// ── Phase 1 checkpoint: incremental + error-tolerance ────────────────────────

/// Modifying file_b must not cause def_use_edges(file_a) to re-execute.
/// A syntax error in file_b must not prevent def_use_edges(file_a) from succeeding.
#[test]
fn incremental_and_error_tolerant() {
    let (mut db, executions) = tracking_db();

    let src_a = "def foo(x):\n    return x\n";
    let src_b = "def bar(y):\n    return y\n";

    let file_a = SourceFile::new(&db, src_a.to_string());
    let file_b = SourceFile::new(&db, src_b.to_string());

    // Warm both caches.
    let edges_a = def_use_edges(&db, file_a);
    let _edges_b = def_use_edges(&db, file_b);

    // Change file_b to syntactically broken source.
    file_b.set_contents(&mut db).to("def bar(\n    return ???\n".to_string());

    // Querying file_a with a fresh execution counter:
    // It must return the memoized result (no re-execution).
    *executions.lock().unwrap() = 0;
    let edges_a2 = def_use_edges(&db, file_a);
    assert_eq!(edges_a, edges_a2, "file_a result unchanged after file_b breaks");
    assert_eq!(
        *executions.lock().unwrap(),
        0,
        "file_a queries must not re-execute when only file_b changes"
    );

    // Querying the broken file_b must succeed (no panic) and re-execute
    // (because its source changed).
    *executions.lock().unwrap() = 0;
    let _edges_b2 = def_use_edges(&db, file_b);
    assert!(
        *executions.lock().unwrap() > 0,
        "file_b queries re-executed after its source changed"
    );
}
