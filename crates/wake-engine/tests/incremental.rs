use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_engine::{Db, SourceFile};
use wake_extract_py::extract_file;

#[salsa::db]
struct TrackingDb {
    storage: salsa::Storage<Self>,
}

#[salsa::db]
impl salsa::Database for TrackingDb {}

#[salsa::db]
impl Db for TrackingDb {}

fn tracking_db() -> (TrackingDb, Arc<Mutex<usize>>) {
    let count = Arc::new(Mutex::new(0usize));
    let count_cb = count.clone();
    let storage = salsa::Storage::new(Some(Box::new(move |event: salsa::Event| {
        if matches!(event.kind, salsa::EventKind::WillExecute { .. }) {
            *count_cb.lock().unwrap() += 1;
        }
    })));
    (TrackingDb { storage }, count)
}

/// Phase 0 checkpoint: only affected queries recompute on edit.
/// Uses extract_file (the Phase 1 extraction query) to prove the salsa
/// incrementality machinery still holds as the codebase grows.
#[test]
fn only_affected_queries_recompute() {
    let (mut db, executions) = tracking_db();

    let file_a = SourceFile::new(&db, "def foo(x): return x".to_string());
    let file_b = SourceFile::new(&db, "def bar(y): return y".to_string());

    // First run — both files parse fresh.
    *executions.lock().unwrap() = 0;
    let result_a = extract_file(&db, file_a);
    let _result_b = extract_file(&db, file_b);
    assert_eq!(*executions.lock().unwrap(), 2, "both files extracted on first run");

    // Modify file_b only — extract_file(file_a) must NOT re-execute.
    *executions.lock().unwrap() = 0;
    file_b.set_contents(&mut db).to("def bar(y): return y + 1".to_string());
    let result_a2 = extract_file(&db, file_a);
    let _result_b2 = extract_file(&db, file_b);
    assert_eq!(result_a, result_a2, "file_a result is unchanged");
    assert_eq!(
        *executions.lock().unwrap(),
        1,
        "only file_b re-executed when file_b changes"
    );

    // Modify file_a — extract_file(file_a) MUST re-execute.
    *executions.lock().unwrap() = 0;
    file_a
        .set_contents(&mut db)
        .to("def foo(x, y): return x + y".to_string());
    let result_a3 = extract_file(&db, file_a);
    assert_eq!(
        *executions.lock().unwrap(),
        1,
        "file_a re-executed when file_a changes"
    );
    assert_ne!(result_a, result_a3, "result changes when signature changes");
}
