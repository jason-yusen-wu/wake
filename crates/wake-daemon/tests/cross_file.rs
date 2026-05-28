//! Daemon-level cross-file analysis: a regression whose None source is in
//! another registered file must surface for the importing file.

use serde_json::{json, Value};
use wake_daemon::{Daemon, Request};

fn req(method: &str, params: Value) -> Request {
    Request { jsonrpc: "2.0".to_string(), method: method.to_string(), params, id: Some(json!(1)) }
}

fn did_change(d: &mut Daemon, uri: &str, text: &str) {
    let r = d.handle(&req("workspace/didChange", json!({ "uri": uri, "text": text })));
    assert!(r.error.is_none());
}

fn regression_count(d: &mut Daemon, uri: &str) -> usize {
    let r = d.handle(&req("analyze/regressions", json!({ "uri": uri })));
    assert!(r.error.is_none(), "analyze error: {:?}", r.error.as_ref().map(|e| &e.message));
    r.result.unwrap()["regressions"].as_array().map(|a| a.len()).unwrap_or(0)
}

#[test]
fn cross_file_regression_surfaces_for_importer() {
    let mut d = Daemon::default();
    did_change(&mut d, "b.py", "def src():\n    return None\n");
    did_change(&mut d, "a.py", "def caller():\n    x = src()\n    return x.attr\n");

    // The regression lives in a.py (the consumer), sourced from b.py.
    assert_eq!(regression_count(&mut d, "a.py"), 1, "cross-file None flow detected for a.py");
    // b.py itself has no consumer regression.
    assert_eq!(regression_count(&mut d, "b.py"), 0, "b.py has no consumer of its own");
}

#[test]
fn editing_source_file_clears_importer_regression() {
    let mut d = Daemon::default();
    did_change(&mut d, "b.py", "def src():\n    return None\n");
    did_change(&mut d, "a.py", "def caller():\n    x = src()\n    return x.attr\n");
    assert_eq!(regression_count(&mut d, "a.py"), 1);

    // Fix the source: src no longer returns None.
    did_change(&mut d, "b.py", "def src():\n    return 1\n");
    assert_eq!(regression_count(&mut d, "a.py"), 0, "editing b.py clears a.py's regression");
}

#[test]
fn blast_radius_spans_files() {
    let mut d = Daemon::default();
    did_change(&mut d, "b.py", "def src():\n    return 1\n");
    did_change(&mut d, "a.py", "def caller():\n    x = src()\n    return x.attr\n");

    // Previewing a change to b.py (now returning None) should introduce a new
    // regression that lives in a.py — a cross-file blast radius.
    let r = d.handle(&req(
        "analyze/blastRadius",
        json!({ "uri": "b.py", "text": "def src():\n    return None\n" }),
    ));
    assert!(r.error.is_none());
    let result = r.result.unwrap();
    let new = result["new_regressions"].as_array().unwrap();
    assert_eq!(new.len(), 1, "editing b.py introduces a regression in a.py (cross-file blast)");

    // Preview must not commit: a.py still clean.
    assert_eq!(regression_count(&mut d, "a.py"), 0, "blastRadius is a non-committing preview");
}
