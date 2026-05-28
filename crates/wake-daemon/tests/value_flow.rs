//! #7: query/valueFlow retrieval mode.

use serde_json::{json, Value};
use wake_daemon::{Daemon, Request, ERR_PARAMS};

fn req(method: &str, params: Value) -> Request {
    Request { jsonrpc: "2.0".to_string(), method: method.to_string(), params, id: Some(json!(1)) }
}

fn did_change(d: &mut Daemon, uri: &str, text: &str) {
    let r = d.handle(&req("workspace/didChange", json!({ "uri": uri, "text": text })));
    assert!(r.error.is_none());
}

fn value_flow(d: &mut Daemon, uri: &str, position: u32, direction: &str) -> Vec<(u64, u64)> {
    let r = d.handle(&req(
        "query/valueFlow",
        json!({ "uri": uri, "position": position, "direction": direction }),
    ));
    assert!(r.error.is_none(), "valueFlow error: {:?}", r.error.as_ref().map(|e| &e.message));
    r.result.unwrap()["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| (n[0].as_u64().unwrap(), n[1].as_u64().unwrap()))
        .collect()
}

// Source byte layout:
//   "def f(x):\n"      -> x (param) def at byte 6
//   "    y = x\n"      -> y def at 14, x use at 18
//   "    return y\n"   -> y use at 31
const SRC: &str = "def f(x):\n    y = x\n    return y\n";

#[test]
fn backward_from_use_returns_def() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    // position 18 is the use of x in `y = x` → its reaching def is the param x (6..7)
    let nodes = value_flow(&mut d, "f.py", 18, "backward");
    assert!(nodes.contains(&(6, 7)), "backward from use of x → param def: {nodes:?}");
}

#[test]
fn forward_from_def_returns_use() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    // position 6 is the param def of x → it flows forward to the use at 18..19
    let nodes = value_flow(&mut d, "f.py", 6, "forward");
    assert!(nodes.contains(&(18, 19)), "forward from param x → use: {nodes:?}");
}

#[test]
fn both_directions_union() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    // position 14 is the def of y → forward to its use at 31..32
    let nodes = value_flow(&mut d, "f.py", 14, "both");
    assert!(nodes.contains(&(31, 32)), "y def flows to its use: {nodes:?}");
}

#[test]
fn position_with_no_flow_is_empty() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    // byte 0 ('d' of `def`) is not a def/use node
    let nodes = value_flow(&mut d, "f.py", 0, "both");
    assert!(nodes.is_empty(), "no value flow at a keyword position: {nodes:?}");
}

#[test]
fn missing_position_errors() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    let r = d.handle(&req("query/valueFlow", json!({ "uri": "f.py", "direction": "both" })));
    assert_eq!(r.error.unwrap().code, ERR_PARAMS);
}

#[test]
fn bad_direction_errors() {
    let mut d = Daemon::default();
    did_change(&mut d, "f.py", SRC);
    let r = d.handle(&req(
        "query/valueFlow",
        json!({ "uri": "f.py", "position": 6, "direction": "sideways" }),
    ));
    assert_eq!(r.error.unwrap().code, ERR_PARAMS);
}

#[test]
fn unknown_file_errors() {
    let mut d = Daemon::default();
    let r = d.handle(&req(
        "query/valueFlow",
        json!({ "uri": "nope.py", "position": 6, "direction": "both" }),
    ));
    assert_eq!(r.error.unwrap().code, ERR_PARAMS);
}
