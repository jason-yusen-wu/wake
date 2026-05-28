//! Cross-file (workspace) value-flow analysis.

use salsa::Setter;
use wake_diff::{workspace_regressions_with_witnesses, WitnessStep};
use wake_engine::{Db, SourceFile, Workspace};
use wake_prop_null::workspace_regressions;
use wake_schema::NullRegression;

#[salsa::db]
#[derive(Default)]
struct TestDb {
    storage: salsa::Storage<Self>,
}
#[salsa::db]
impl salsa::Database for TestDb {}
#[salsa::db]
impl Db for TestDb {}

fn workspace(db: &TestDb, files: &[(&str, &str)]) -> Workspace {
    let entries: Vec<(String, SourceFile)> = files
        .iter()
        .map(|(path, src)| (path.to_string(), SourceFile::new(db, src.to_string())))
        .collect();
    Workspace::new(db, entries)
}

fn regs(db: &TestDb, ws: Workspace) -> Vec<NullRegression> {
    workspace_regressions(db, ws)
}

fn kinds(steps: &[WitnessStep]) -> Vec<&'static str> {
    steps
        .iter()
        .map(|s| match s {
            WitnessStep::NullableParam { .. } => "NullableParam",
            WitnessStep::NoneAssignment { .. } => "NoneAssignment",
            WitnessStep::VariableCopy { .. } => "VariableCopy",
            WitnessStep::CallReturn { .. } => "CallReturn",
            WitnessStep::Consumer { .. } => "Consumer",
            WitnessStep::Opaque { .. } => "Opaque",
        })
        .collect()
}

// ── Return-value flow across files (`from b import src; src()`) ───────────────

#[test]
fn cross_file_return_flow() {
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("b.py", "def src():\n    return None\n"),
            ("a.py", "def caller():\n    x = src()\n    return x.attr\n"),
        ],
    );
    let r = regs(&db, ws);
    assert_eq!(r.len(), 1, "src() returns None across files → x.attr is a regression");
    assert_eq!(r[0].file, "a.py", "regression attributed to the consuming file");
    assert_eq!(r[0].object_symbol, "x");
}

// ── Qualified call across files (`import b; b.src()`) ─────────────────────────

#[test]
fn cross_file_qualified_call() {
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("b.py", "def src():\n    return None\n"),
            ("a.py", "import b\ndef caller():\n    x = b.src()\n    return x.attr\n"),
        ],
    );
    let r = regs(&db, ws);
    assert_eq!(r.len(), 1, "b.src() resolves cross-file (b is a module, not a local)");
    assert_eq!(r[0].file, "a.py");
}

#[test]
fn qualified_call_on_local_is_not_resolved() {
    // Here `b` IS a local (a parameter), so b.src() is a method call we can't
    // resolve — must NOT pull in the unrelated workspace function `src`.
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("m.py", "def src():\n    return None\n"),
            ("a.py", "def caller(b):\n    x = b.src()\n    return x.attr\n"),
        ],
    );
    let r = regs(&db, ws);
    assert_eq!(r.len(), 0, "b.src() on a local parameter is a method call → Unknown");
}

// ── Argument-into-callee deref across files (case B, file-attributed) ─────────

#[test]
fn cross_file_arg_into_callee() {
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("b.py", "def consumer(x):\n    return x.attr\n"),
            ("a.py", "def caller():\n    consumer(None)\n"),
        ],
    );
    let r = regs(&db, ws);
    assert_eq!(r.len(), 1, "None passed into consumer in another file → x.attr regression");
    assert_eq!(r[0].file, "b.py", "regression lives in the callee's file");
    assert_eq!(r[0].object_symbol, "x");
}

// ── Ambiguity declines (precision over soundness) ────────────────────────────

#[test]
fn ambiguous_name_not_resolved() {
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("b.py", "def src():\n    return None\n"),
            ("c.py", "def src():\n    return 1\n"),
            ("a.py", "def caller():\n    x = src()\n    return x.attr\n"),
        ],
    );
    let r = regs(&db, ws);
    assert_eq!(r.len(), 0, "src defined twice → ambiguous → Unknown → no regression");
}

// ── Cross-file witness ───────────────────────────────────────────────────────

#[test]
fn cross_file_witness_traces_into_callee() {
    let db = TestDb::default();
    let ws = workspace(
        &db,
        &[
            ("b.py", "def src():\n    return None\n"),
            ("a.py", "def caller():\n    x = src()\n    return x.attr\n"),
        ],
    );
    let reports = workspace_regressions_with_witnesses(&db, ws);
    assert_eq!(reports.len(), 1);
    assert_eq!(
        kinds(&reports[0].witness),
        vec!["NoneAssignment", "CallReturn", "Consumer"],
        "witness traces None across the file boundary"
    );
}

// ── Incrementality: editing the callee file flips the importer's regression ───

#[test]
fn editing_callee_file_updates_importer() {
    let mut db = TestDb::default();
    let src_file = SourceFile::new(&db, "def src():\n    return None\n".to_string());
    let caller_file =
        SourceFile::new(&db, "def caller():\n    x = src()\n    x.attr\n".to_string());
    let ws = Workspace::new(
        &db,
        vec![("b.py".to_string(), src_file), ("a.py".to_string(), caller_file)],
    );

    assert_eq!(workspace_regressions(&db, ws).len(), 1, "src returns None → regression");

    // src now returns a non-None value.
    src_file.set_contents(&mut db).to("def src():\n    return 1\n".to_string());
    assert_eq!(
        workspace_regressions(&db, ws).len(),
        0,
        "after callee no longer returns None, the cross-file regression is gone"
    );
}
