use std::collections::HashMap;

use salsa::Setter;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use wake_diff::{diff_results, regressions_with_witnesses, WitnessStep};
use wake_engine::{Database, SourceFile};
use wake_feedback::{shape_feedback, AffectedConsumer, Confidence, RootCause, ShapedFeedback};
use wake_ir::def_use_edges;
use wake_schema::{ConsumerKind, NodeId, NullRegression};

// ── JSON-RPC 2.0 protocol types ───────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct Request {
    pub jsonrpc: String,
    pub method: String,
    #[serde(default)]
    pub params: Value,
    pub id: Option<Value>,
}

#[derive(Debug, Serialize)]
pub struct Response {
    pub jsonrpc: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<RpcError>,
    pub id: Value,
}

#[derive(Debug, Serialize)]
pub struct RpcError {
    pub code: i32,
    pub message: String,
}

// Standard JSON-RPC 2.0 error codes
pub const ERR_PARSE: i32 = -32700;
pub const ERR_INVALID: i32 = -32600;
pub const ERR_METHOD: i32 = -32601;
pub const ERR_PARAMS: i32 = -32602;
pub const ERR_INTERNAL: i32 = -32603;

impl Response {
    pub fn ok(id: Value, result: Value) -> Self {
        Self { jsonrpc: "2.0", result: Some(result), error: None, id }
    }

    pub fn err(id: Value, code: i32, message: impl Into<String>) -> Self {
        Self {
            jsonrpc: "2.0",
            result: None,
            error: Some(RpcError { code, message: message.into() }),
            id,
        }
    }
}

// ── Daemon state ──────────────────────────────────────────────────────────────

/// Persistent workspace state: a salsa Database plus a registry of open files.
///
/// The Database holds all memoized analysis results; updating a file's text via
/// `set_contents` invalidates only the queries that depend on it, keeping all
/// other results warm.
#[derive(Default)]
pub struct Daemon {
    db: Database,
    /// uri → salsa SourceFile handle
    files: HashMap<String, SourceFile>,
    /// uri → current source text (kept to support blast-radius preview + restore)
    texts: HashMap<String, String>,
}

impl Daemon {
    /// Dispatch one JSON-RPC request and return the JSON-RPC response.
    pub fn handle(&mut self, req: &Request) -> Response {
        if req.jsonrpc != "2.0" {
            let id = req.id.clone().unwrap_or(Value::Null);
            return Response::err(id, ERR_INVALID, "jsonrpc must be \"2.0\"");
        }
        let id = req.id.clone().unwrap_or(Value::Null);
        match req.method.as_str() {
            "workspace/didChange" => self.handle_did_change(&req.params, id),
            "analyze/regressions" => self.handle_regressions(&req.params, id),
            "analyze/blastRadius" => self.handle_blast_radius(&req.params, id),
            "query/valueFlow" => self.handle_value_flow(&req.params, id),
            other => Response::err(id, ERR_METHOD, format!("unknown method: {other}")),
        }
    }

    // ── workspace/didChange ───────────────────────────────────────────────────

    fn handle_did_change(&mut self, params: &Value, id: Value) -> Response {
        let uri = match params["uri"].as_str() {
            Some(u) => u.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'uri'"),
        };
        let text = match params["text"].as_str() {
            Some(t) => t.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'text'"),
        };
        self.upsert_file(&uri, text);
        Response::ok(id, serde_json::json!({ "ok": true }))
    }

    // ── analyze/regressions ───────────────────────────────────────────────────

    fn handle_regressions(&mut self, params: &Value, id: Value) -> Response {
        let uri = match params["uri"].as_str() {
            Some(u) => u.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'uri'"),
        };
        let file = match self.files.get(&uri).copied() {
            Some(f) => f,
            None => return Response::err(id, ERR_PARAMS, format!("unknown file: {uri}")),
        };

        let reports = regressions_with_witnesses(&self.db, file);
        let shaped = shape_feedback(&reports);

        Response::ok(
            id,
            serde_json::json!({
                "regressions": shaped.iter().map(serialize_feedback).collect::<Vec<_>>(),
            }),
        )
    }

    // ── analyze/blastRadius ───────────────────────────────────────────────────
    //
    // Preview: compute the diff of applying `text` to `uri` WITHOUT committing it.
    // After this call the database is back in the same state as before.

    fn handle_blast_radius(&mut self, params: &Value, id: Value) -> Response {
        let uri = match params["uri"].as_str() {
            Some(u) => u.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'uri'"),
        };
        let new_text = match params["text"].as_str() {
            Some(t) => t.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'text'"),
        };
        let file = match self.files.get(&uri).copied() {
            Some(f) => f,
            None => return Response::err(id, ERR_PARAMS, format!("unknown file: {uri}")),
        };
        let old_text = self.texts[&uri].clone();

        // Before: current database state.
        let before = regressions_with_witnesses(&self.db, file);

        // Apply the proposed new text.
        file.set_contents(&mut self.db).to(new_text);
        let after = regressions_with_witnesses(&self.db, file);

        // Diff, then restore the original text.
        let diff = diff_results(&before, &after);
        file.set_contents(&mut self.db).to(old_text);

        let new_shaped = shape_feedback(&diff.new_regressions);

        Response::ok(
            id,
            serde_json::json!({
                "blast_radius": diff.blast_radius.iter().map(serialize_node).collect::<Vec<_>>(),
                "new_regressions": new_shaped.iter().map(serialize_feedback).collect::<Vec<_>>(),
                "fixed_regressions": diff.fixed_regressions.iter().map(serialize_fixed).collect::<Vec<_>>(),
            }),
        )
    }

    // ── query/valueFlow (retrieval mode) ──────────────────────────────────────
    //
    // Non-differential retrieval: given a byte position, return the def-use
    // related nodes. `direction`:
    //   "backward" — definitions that reach the use at `position`
    //   "forward"  — uses reached by the definition at `position`
    //   "both"     — the union (default)

    fn handle_value_flow(&mut self, params: &Value, id: Value) -> Response {
        let uri = match params["uri"].as_str() {
            Some(u) => u.to_string(),
            None => return Response::err(id, ERR_PARAMS, "missing 'uri'"),
        };
        let position = match params["position"].as_u64() {
            Some(p) => p as u32,
            None => return Response::err(id, ERR_PARAMS, "missing or invalid 'position'"),
        };
        let direction = params["direction"].as_str().unwrap_or("both");
        if !matches!(direction, "backward" | "forward" | "both") {
            return Response::err(id, ERR_PARAMS, "'direction' must be backward|forward|both");
        }
        let file = match self.files.get(&uri).copied() {
            Some(f) => f,
            None => return Response::err(id, ERR_PARAMS, format!("unknown file: {uri}")),
        };

        let want_back = direction != "forward";
        let want_fwd = direction != "backward";
        let contains = |n: NodeId| n.start_byte <= position && position < n.end_byte;

        let mut nodes: Vec<NodeId> = Vec::new();
        for (_func, edges) in def_use_edges(&self.db, file) {
            for e in edges {
                if want_back && contains(e.use_node) {
                    nodes.push(e.def_node);
                }
                if want_fwd && contains(e.def_node) {
                    nodes.push(e.use_node);
                }
            }
        }
        nodes.sort();
        nodes.dedup();

        Response::ok(
            id,
            serde_json::json!({
                "nodes": nodes.iter().map(serialize_node).collect::<Vec<_>>(),
            }),
        )
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    fn upsert_file(&mut self, uri: &str, text: String) {
        if let Some(file) = self.files.get(uri).copied() {
            file.set_contents(&mut self.db).to(text.clone());
        } else {
            let file = SourceFile::new(&self.db, text.clone());
            self.files.insert(uri.to_string(), file);
        }
        self.texts.insert(uri.to_string(), text);
    }
}

// ── JSON serialisation helpers ────────────────────────────────────────────────

fn serialize_node(n: &NodeId) -> Value {
    serde_json::json!([n.start_byte, n.end_byte])
}

fn serialize_confidence(c: Confidence) -> &'static str {
    match c {
        Confidence::High => "high",
        Confidence::Medium => "medium",
        Confidence::Low => "low",
    }
}

fn serialize_root_cause(rc: &RootCause) -> Value {
    match rc {
        RootCause::NoneAssignment { node, symbol } => serde_json::json!({
            "kind": "none_assignment",
            "symbol": symbol,
            "byte_range": serialize_node(node),
        }),
        RootCause::NullableParam { node, symbol } => serde_json::json!({
            "kind": "nullable_param",
            "symbol": symbol,
            "byte_range": serialize_node(node),
        }),
        RootCause::Opaque { description } => serde_json::json!({
            "kind": "opaque",
            "description": description,
        }),
    }
}

fn serialize_consumer_kind(k: ConsumerKind) -> &'static str {
    match k {
        ConsumerKind::Attribute => "attribute",
        ConsumerKind::Subscript => "subscript",
        ConsumerKind::Call => "call",
    }
}

fn serialize_witness_step(step: &WitnessStep) -> Value {
    match step {
        WitnessStep::NoneAssignment { node, symbol } => serde_json::json!({
            "kind": "none_assignment",
            "symbol": symbol,
            "byte_range": serialize_node(node),
        }),
        WitnessStep::NullableParam { node, symbol } => serde_json::json!({
            "kind": "nullable_param",
            "symbol": symbol,
            "byte_range": serialize_node(node),
        }),
        WitnessStep::VariableCopy { node, from, to } => serde_json::json!({
            "kind": "variable_copy",
            "from": from,
            "to": to,
            "byte_range": serialize_node(node),
        }),
        WitnessStep::CallReturn { node, callee, to } => serde_json::json!({
            "kind": "call_return",
            "callee": callee,
            "to": to,
            "byte_range": serialize_node(node),
        }),
        WitnessStep::Consumer { node, symbol, kind } => serde_json::json!({
            "kind": "consumer",
            "symbol": symbol,
            "consumer_kind": serialize_consumer_kind(*kind),
            "byte_range": serialize_node(node),
        }),
        WitnessStep::Opaque { symbol } => serde_json::json!({
            "kind": "opaque",
            "symbol": symbol,
        }),
    }
}

fn serialize_consumer(c: &AffectedConsumer) -> Value {
    serde_json::json!({
        "symbol": c.symbol,
        "kind": serialize_consumer_kind(c.kind),
        "byte_range": serialize_node(&c.node),
        "witness": c.witness.iter().map(serialize_witness_step).collect::<Vec<_>>(),
    })
}

fn serialize_feedback(f: &ShapedFeedback) -> Value {
    serde_json::json!({
        "root_cause": serialize_root_cause(&f.root_cause),
        "consumers": f.consumers.iter().map(serialize_consumer).collect::<Vec<_>>(),
        "confidence": serialize_confidence(f.confidence),
        "fix_locus": f.fix_locus().map(|n| serialize_node(&n)),
    })
}

fn serialize_fixed(r: &NullRegression) -> Value {
    serde_json::json!({
        "symbol": r.object_symbol,
        "consumer": serialize_node(&r.consumer_node),
    })
}

// ── Line-protocol helpers (used by main.rs) ───────────────────────────────────

/// Parse one newline-delimited JSON-RPC request line.
/// Returns `Err(Response)` if the line is malformed.
pub fn parse_request(line: &str) -> Result<Request, Response> {
    serde_json::from_str(line).map_err(|e| {
        Response::err(Value::Null, ERR_PARSE, format!("parse error: {e}"))
    })
}
