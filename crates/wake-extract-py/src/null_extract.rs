use tree_sitter::Node;
use wake_engine::{Db, SourceFile};
use wake_schema::{
    CallArgKind, Consumer, ConsumerKind, NullCallSite, NullDef, NullFact, NullFileFacts,
    NullFunctionFacts, NullReturn, NullabilityValue, RhsNullability,
};

use crate::{node_id, node_text};

/// Extract nullability-relevant facts from a Python file.
///
/// This is the M-side of the M+N decomposition for the nullability property:
/// all Python-specific knowledge (what `Optional[T]` means, what `None` is,
/// which constructs are consumer sites) lives here and only here.
#[salsa::tracked]
pub fn extract_null_file(db: &dyn Db, file: SourceFile) -> NullFileFacts {
    let src = file.contents(db);
    extract_null_source(src.as_bytes())
}

pub fn extract_null_source(src: &[u8]) -> NullFileFacts {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .expect("failed to load Python grammar");
    let tree = parser.parse(src, None).expect("tree-sitter parse cancelled");
    let root = tree.root_node();

    let mut file_facts = NullFileFacts::default();
    let mut cursor = root.walk();
    for node in root.children(&mut cursor) {
        if node.kind() == "function_definition" {
            file_facts.functions.push(extract_null_function(src, node));
        }
        // Module-level constructs outside functions are out of Phase 2 scope.
    }
    file_facts
}

fn extract_null_function(src: &[u8], func_node: Node<'_>) -> NullFunctionFacts {
    let func_name = func_node
        .child_by_field_name("name")
        .map(|n| node_text(src, n).to_string())
        .unwrap_or_default();

    let mut facts: Vec<NullFact> = Vec::new();

    let mut cursor = func_node.walk();
    for child in func_node.children(&mut cursor) {
        match child.kind() {
            "parameters" => extract_null_params(src, child, &mut facts),
            "block" => extract_null_block(src, child, &mut facts),
            "ERROR" => facts.push(NullFact::Unknown(node_id(child))),
            _ => {}
        }
    }

    NullFunctionFacts { func_node: node_id(func_node), func_name, facts }
}

// ── Parameter extraction ──────────────────────────────────────────────────────

fn extract_null_params(src: &[u8], params: Node<'_>, facts: &mut Vec<NullFact>) {
    let mut cursor = params.walk();
    for child in params.children(&mut cursor) {
        match child.kind() {
            "identifier" => {
                // Unannotated parameter → Unknown nullability.
                facts.push(NullFact::Param(NullDef {
                    node: node_id(child),
                    symbol: node_text(src, child).to_string(),
                    annotation: NullabilityValue::Unknown,
                    rhs: RhsNullability::Unknown,
                }));
            }
            "typed_parameter" => {
                // tree-sitter-python: typed_parameter has no "name" field;
                // the identifier is the first named child.
                let name = child.named_child(0);
                let type_node = child.child_by_field_name("type");
                if let Some(name) = name
                    && name.kind() == "identifier"
                {
                    let annotation = type_node
                        .map(|t| parse_annotation_text(node_text(src, t)))
                        .unwrap_or(NullabilityValue::Unknown);
                    facts.push(NullFact::Param(NullDef {
                        node: node_id(name),
                        symbol: node_text(src, name).to_string(),
                        annotation,
                        rhs: RhsNullability::Unknown,
                    }));
                }
            }
            "default_parameter" => {
                // def f(x=default): x is Unknown (default might or might not be None)
                if let Some(name) = child.child_by_field_name("name")
                    && name.kind() == "identifier"
                {
                    facts.push(NullFact::Param(NullDef {
                        node: node_id(name),
                        symbol: node_text(src, name).to_string(),
                        annotation: NullabilityValue::Unknown,
                        rhs: RhsNullability::Unknown,
                    }));
                }
            }
            "typed_default_parameter" => {
                let name = child.child_by_field_name("name");
                let type_node = child.child_by_field_name("type");
                if let Some(name) = name
                    && name.kind() == "identifier"
                {
                    let annotation = type_node
                        .map(|t| parse_annotation_text(node_text(src, t)))
                        .unwrap_or(NullabilityValue::Unknown);
                    facts.push(NullFact::Param(NullDef {
                        node: node_id(name),
                        symbol: node_text(src, name).to_string(),
                        annotation,
                        rhs: RhsNullability::Unknown,
                    }));
                }
            }
            // *args, **kwargs — treated as Unknown
            "list_splat_pattern" | "dictionary_splat_pattern" => {
                if let Some(inner) = child.child(1)
                    && inner.kind() == "identifier"
                {
                    facts.push(NullFact::Param(NullDef {
                        node: node_id(inner),
                        symbol: node_text(src, inner).to_string(),
                        annotation: NullabilityValue::Unknown,
                        rhs: RhsNullability::Unknown,
                    }));
                }
            }
            "ERROR" => facts.push(NullFact::Unknown(node_id(child))),
            _ => {}
        }
    }
}

// ── Block / statement extraction ──────────────────────────────────────────────

fn extract_null_block(src: &[u8], block: Node<'_>, facts: &mut Vec<NullFact>) {
    let mut cursor = block.walk();
    for stmt in block.children(&mut cursor) {
        extract_null_stmt(src, stmt, facts);
    }
}

fn extract_null_stmt(src: &[u8], stmt: Node<'_>, facts: &mut Vec<NullFact>) {
    match stmt.kind() {
        "expression_statement" => {
            if let Some(inner) = stmt.child(0) {
                match inner.kind() {
                    "assignment" => extract_null_assignment(src, inner, facts),
                    "augmented_assignment" => {
                        // x += expr: RHS consumers first, then x is reassigned Unknown.
                        if let Some(rhs) = inner.child_by_field_name("right") {
                            collect_consumers(src, rhs, facts);
                        }
                        if let Some(lhs) = inner.child_by_field_name("left") {
                            if lhs.kind() == "identifier" {
                                facts.push(NullFact::Assign(NullDef {
                                    node: node_id(lhs),
                                    symbol: node_text(src, lhs).to_string(),
                                    annotation: NullabilityValue::Unknown,
                                    rhs: RhsNullability::Unknown,
                                }));
                            } else {
                                // x.attr += …: x is consumed
                                collect_consumers(src, lhs, facts);
                            }
                        }
                    }
                    "call" => {
                        // Bare call statement — may be interprocedural.
                        extract_call_stmt(src, inner, facts);
                    }
                    _ => collect_consumers(src, inner, facts),
                }
            }
        }
        "return_statement" => {
            let mut cursor = stmt.walk();
            for child in stmt.children(&mut cursor) {
                if child.kind() == "return" {
                    continue;
                }
                // Consumer sites in the return expression (handles `return x()` where
                // x is a local Nullable variable — must produce Consumer(x, Call)).
                collect_consumers(src, child, facts);
                // Return-value fact for summary computation.
                facts.push(NullFact::Return(NullReturn {
                    node: node_id(child),
                    rhs: classify_rhs(src, child),
                }));
            }
        }
        // All control-flow constructs are opaque barriers.
        "if_statement"
        | "for_statement"
        | "while_statement"
        | "try_statement"
        | "with_statement"
        | "match_statement" => {
            facts.push(NullFact::Unknown(node_id(stmt)));
        }
        "ERROR" => facts.push(NullFact::Unknown(node_id(stmt))),
        // pass, break, continue, raise, assert, import, del, etc.
        _ => {}
    }
}

/// Emit consumer facts for a bare call statement, plus a `CallStmt` if the
/// callee is a direct identifier (for interprocedural analysis).
///
/// We always call `collect_consumers` so that `x()` where x is a local
/// variable (potentially None) correctly produces Consumer(x, Call).
/// The additional `CallStmt` is only for cross-function regression propagation.
fn extract_call_stmt(src: &[u8], call_node: Node<'_>, facts: &mut Vec<NullFact>) {
    // Always collect — handles the case where the callee is a local Nullable var.
    collect_consumers(src, call_node, facts);

    // Also record the interprocedural call site for summary application.
    if let Some(func) = call_node.child_by_field_name("function")
        && func.kind() == "identifier"
    {
        facts.push(NullFact::CallStmt(NullCallSite {
            node: node_id(call_node),
            callee: node_text(src, func).to_string(),
            args: extract_call_args(src, call_node),
        }));
    }
}

fn extract_null_assignment(src: &[u8], assign: Node<'_>, facts: &mut Vec<NullFact>) {
    let rhs_node = assign.child_by_field_name("right");

    // Evaluation order: RHS consumers first, then define LHS.
    // Always use collect_consumers so that `y = x()` where x is a local
    // Nullable variable produces Consumer(x, Call) — same as other consumer sites.
    if let Some(rhs) = rhs_node {
        collect_consumers(src, rhs, facts);
    }

    if let Some(lhs) = assign.child_by_field_name("left") {
        if lhs.kind() == "identifier" {
            let annotation = assign
                .child_by_field_name("type")
                .map(|t| parse_annotation_text(node_text(src, t)))
                .unwrap_or(NullabilityValue::Unknown);
            let rhs = rhs_node
                .map(|r| classify_rhs(src, r))
                .unwrap_or(RhsNullability::Unknown);
            facts.push(NullFact::Assign(NullDef {
                node: node_id(lhs),
                symbol: node_text(src, lhs).to_string(),
                annotation,
                rhs,
            }));
        } else {
            // Non-identifier LHS (x.attr = …, x[i] = …): the LHS itself may be a consumer.
            collect_consumers(src, lhs, facts);
        }
    }
}

// ── Call argument helpers ─────────────────────────────────────────────────────

/// Extract argument kinds from a call expression's argument list.
fn extract_call_args(src: &[u8], call_node: Node<'_>) -> Vec<CallArgKind> {
    let mut args = Vec::new();
    let Some(arg_list) = call_node.child_by_field_name("arguments") else {
        return args;
    };
    let mut cursor = arg_list.walk();
    for child in arg_list.children(&mut cursor) {
        match child.kind() {
            // Punctuation and delimiters
            "," | "(" | ")" => {}
            "none" => args.push(CallArgKind::NullLiteral),
            "identifier" => args.push(CallArgKind::Var(node_text(src, child).to_string())),
            "true" | "false" | "integer" | "float" | "string" | "concatenated_string"
            | "raw_string_literal" | "bytes" | "list" | "set" | "dictionary" | "tuple" => {
                args.push(CallArgKind::NonNullLiteral);
            }
            // keyword_argument, list_splat, dict_splat, *args, **kwargs → Unknown
            _ => args.push(CallArgKind::Unknown),
        }
    }
    args
}

// ── Consumer extraction ───────────────────────────────────────────────────────

/// Recursively find all consumer sites in an expression.
///
/// A consumer site is a place where a local variable is dereferenced:
///   x.attr   → AttributeError if x is None
///   x[i]     → TypeError if x is None
///   x()      → TypeError if x is None
///
/// The rule: only emit a Consumer when the IMMEDIATE object is an identifier.
/// For chained expressions (x.y.z, x.y()) the innermost identifier is the
/// local variable we can reason about.
pub fn collect_consumers(src: &[u8], node: Node<'_>, facts: &mut Vec<NullFact>) {
    match node.kind() {
        "attribute" => {
            match node.child_by_field_name("object") {
                Some(obj) if obj.kind() == "identifier" => {
                    facts.push(NullFact::Consumer(Consumer {
                        node: node_id(node),
                        object_symbol: node_text(src, obj).to_string(),
                        kind: ConsumerKind::Attribute,
                    }));
                }
                Some(obj) => collect_consumers(src, obj, facts),
                None => {}
            }
        }
        "subscript" => {
            match node.child_by_field_name("value") {
                Some(val) if val.kind() == "identifier" => {
                    facts.push(NullFact::Consumer(Consumer {
                        node: node_id(node),
                        object_symbol: node_text(src, val).to_string(),
                        kind: ConsumerKind::Subscript,
                    }));
                }
                Some(val) => collect_consumers(src, val, facts),
                None => {}
            }
            // Also scan the subscript expression itself.
            if let Some(idx) = node.child_by_field_name("subscript") {
                collect_consumers(src, idx, facts);
            }
        }
        "call" => {
            match node.child_by_field_name("function") {
                Some(func) if func.kind() == "identifier" => {
                    facts.push(NullFact::Consumer(Consumer {
                        node: node_id(node),
                        object_symbol: node_text(src, func).to_string(),
                        kind: ConsumerKind::Call,
                    }));
                }
                Some(func) => {
                    // e.g. x.upper() → recurse into the attribute to find Consumer(x, Attribute)
                    collect_consumers(src, func, facts);
                }
                None => {}
            }
            // Also collect consumers from arguments.
            if let Some(args) = node.child_by_field_name("arguments") {
                let mut cursor = args.walk();
                for child in args.children(&mut cursor) {
                    collect_consumers(src, child, facts);
                }
            }
        }
        // Literals and bare identifiers are not consumer sites.
        "identifier"
        | "integer"
        | "float"
        | "string"
        | "concatenated_string"
        | "true"
        | "false"
        | "none"
        | "ellipsis"
        | "comment" => {}
        "ERROR" => facts.push(NullFact::Unknown(node_id(node))),
        // Everything else: recurse into all children.
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_consumers(src, child, facts);
            }
        }
    }
}

// ── RHS classification ────────────────────────────────────────────────────────

/// Classify the nullability of a right-hand-side expression without running dataflow.
fn classify_rhs(src: &[u8], rhs: Node<'_>) -> RhsNullability {
    match rhs.kind() {
        "none" => RhsNullability::Literal(NullabilityValue::Nullable),
        "true" | "false" | "integer" | "float" | "string" | "concatenated_string"
        | "raw_string_literal" | "bytes" | "list" | "set" | "dictionary" | "tuple" => {
            RhsNullability::Literal(NullabilityValue::NonNull)
        }
        "identifier" => RhsNullability::FromVar(node_text(src, rhs).to_string()),
        "call" => {
            // Direct call to a named function → interprocedural tracking.
            match rhs.child_by_field_name("function") {
                Some(func) if func.kind() == "identifier" => RhsNullability::Call {
                    callee: node_text(src, func).to_string(),
                    args: extract_call_args(src, rhs),
                },
                _ => RhsNullability::Unknown,
            }
        }
        // Function calls, attribute access, binary ops, etc. — all Unknown.
        _ => RhsNullability::Unknown,
    }
}

// ── Type annotation parsing ───────────────────────────────────────────────────

/// Determine the nullability implied by a Python type annotation string.
pub fn parse_annotation_text(text: &str) -> NullabilityValue {
    let t = text.trim();

    if t == "None" {
        return NullabilityValue::Nullable;
    }
    if t.starts_with("Optional[") || t.starts_with("Optional [") {
        return NullabilityValue::Nullable;
    }
    if t.starts_with("Union[") && t.contains("None") {
        return NullabilityValue::Nullable;
    }
    if t.contains("| None") || t.contains("None |") {
        return NullabilityValue::Nullable;
    }
    if is_known_non_nullable(t) {
        return NullabilityValue::NonNull;
    }

    NullabilityValue::Unknown
}

fn is_known_non_nullable(t: &str) -> bool {
    matches!(
        t,
        "str" | "int" | "float" | "bool" | "bytes" | "bytearray"
            | "list" | "dict" | "set" | "tuple" | "frozenset"
            | "object" | "type" | "complex"
    ) || {
        let prefixes = [
            "List[", "Dict[", "Set[", "FrozenSet[", "Tuple[", "Sequence[",
            "Iterable[", "Iterator[", "Generator[", "Callable[", "Type[",
            "Deque[", "DefaultDict[", "Counter[", "ChainMap[",
        ];
        prefixes.iter().any(|p| t.starts_with(p)) && !t.contains("None")
    }
}
