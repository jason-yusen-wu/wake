pub mod null_extract;
pub use null_extract::extract_null_file;

use tree_sitter::Node;
use wake_engine::{Db, SourceFile};
use wake_schema::{Def, DefKind, Fact, FileFacts, FunctionFacts, NodeId, Unknown, Use};

/// Extract schema facts from a Python source file.
///
/// All Python-specific tree-sitter logic lives here and only here.
/// The returned `FileFacts` uses only schema types, keeping the narrow waist clean.
#[salsa::tracked]
pub fn extract_file(db: &dyn Db, file: SourceFile) -> FileFacts {
    let src = file.contents(db);
    extract_source(src.as_bytes())
}

pub fn node_id(node: Node<'_>) -> NodeId {
    NodeId {
        start_byte: node.start_byte() as u32,
        end_byte: node.end_byte() as u32,
    }
}

pub fn node_text<'s>(src: &'s [u8], node: Node<'_>) -> &'s str {
    std::str::from_utf8(&src[node.byte_range()]).unwrap_or("<invalid utf8>")
}

pub fn extract_source(src: &[u8]) -> FileFacts {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .expect("failed to load Python grammar");
    let tree = parser.parse(src, None).expect("tree-sitter parse cancelled");
    let root = tree.root_node();

    let mut file_facts = FileFacts::default();
    let mut cursor = root.walk();
    for node in root.children(&mut cursor) {
        match node.kind() {
            "function_definition" => {
                file_facts.functions.push(extract_function(src, node));
            }
            "ERROR" => {
                file_facts.file_unknowns.push(Unknown {
                    node: node_id(node),
                    reason: "parse error at module level".to_string(),
                });
            }
            // Module-level statements other than function_definition are out of
            // scope for Phase 1. They don't produce facts.
            _ => {}
        }
    }
    file_facts
}

fn extract_function(src: &[u8], func_node: Node<'_>) -> FunctionFacts {
    let mut facts: Vec<Fact> = Vec::new();

    // Scan direct children so we catch ERROR nodes that tree-sitter places
    // between named fields (e.g. between parameters and body on broken input).
    let mut cursor = func_node.walk();
    for child in func_node.children(&mut cursor) {
        match child.kind() {
            // Named fields are handled below; skip them in this pass.
            "def" | "identifier" | ":" | "comment" | "decorator" => {}
            "parameters" => extract_parameters(src, child, &mut facts),
            "block" => extract_block(src, child, &mut facts),
            "ERROR" => {
                facts.push(Fact::Unknown(Unknown {
                    node: node_id(child),
                    reason: "parse error in function definition".to_string(),
                }));
            }
            _ => {}
        }
    }

    FunctionFacts { func_node: node_id(func_node), facts }
}

fn extract_parameters(src: &[u8], params: Node<'_>, facts: &mut Vec<Fact>) {
    let mut cursor = params.walk();
    for child in params.children(&mut cursor) {
        match child.kind() {
            "identifier" => {
                facts.push(Fact::Def(Def {
                    node: node_id(child),
                    symbol: node_text(src, child).to_string(),
                    kind: DefKind::Parameter,
                }));
            }
            // typed_parameter: (name: identifier, type: ...) — pull out the name
            "typed_parameter" | "default_parameter" => {
                if let Some(name) = child.child_by_field_name("name")
                    && name.kind() == "identifier"
                {
                    facts.push(Fact::Def(Def {
                        node: node_id(name),
                        symbol: node_text(src, name).to_string(),
                        kind: DefKind::Parameter,
                    }));
                }
            }
            "list_splat_pattern" | "dictionary_splat_pattern" => {
                // *args / **kwargs — extract inner identifier
                if let Some(inner) = child.child(1)
                    && inner.kind() == "identifier"
                {
                    facts.push(Fact::Def(Def {
                        node: node_id(inner),
                        symbol: node_text(src, inner).to_string(),
                        kind: DefKind::Parameter,
                    }));
                }
            }
            "ERROR" => {
                facts.push(Fact::Unknown(Unknown {
                    node: node_id(child),
                    reason: "parse error in parameter list".to_string(),
                }));
            }
            // Punctuation: "(", ")", ",", "self" keyword, etc.
            _ => {}
        }
    }
}

/// Walk a block (sequence of statements) in execution order.
fn extract_block(src: &[u8], block: Node<'_>, facts: &mut Vec<Fact>) {
    let mut cursor = block.walk();
    for stmt in block.children(&mut cursor) {
        extract_stmt(src, stmt, facts);
    }
}

fn extract_stmt(src: &[u8], stmt: Node<'_>, facts: &mut Vec<Fact>) {
    match stmt.kind() {
        "expression_statement" => {
            // May contain an assignment or a bare expression.
            if let Some(inner) = stmt.child(0) {
                match inner.kind() {
                    "assignment" => {
                        // RHS uses first (they are evaluated before the LHS is bound).
                        if let Some(rhs) = inner.child_by_field_name("right") {
                            collect_uses(src, rhs, facts);
                        }
                        // Then the LHS def.
                        if let Some(lhs) = inner.child_by_field_name("left") {
                            extract_assign_target(src, lhs, facts);
                        }
                    }
                    "augmented_assignment" => {
                        // x += expr: x is both a use and a def.
                        if let Some(lhs) = inner.child_by_field_name("left")
                            && lhs.kind() == "identifier"
                        {
                            // Use of the current value
                            facts.push(Fact::Use(Use {
                                node: node_id(lhs),
                                symbol: node_text(src, lhs).to_string(),
                            }));
                        }
                        if let Some(rhs) = inner.child_by_field_name("right") {
                            collect_uses(src, rhs, facts);
                        }
                        if let Some(lhs) = inner.child_by_field_name("left") {
                            extract_assign_target(src, lhs, facts);
                        }
                    }
                    _ => collect_uses(src, inner, facts),
                }
            }
        }
        "return_statement" => {
            let mut cursor = stmt.walk();
            for child in stmt.children(&mut cursor) {
                if child.kind() != "return" {
                    collect_uses(src, child, facts);
                }
            }
        }
        // Control flow: we declare ignorance for all branching constructs.
        // The downstream IR treats Unknown as a barrier — defs before may or
        // may not reach uses after. Phase 2 will handle branch joins properly.
        "if_statement"
        | "for_statement"
        | "while_statement"
        | "try_statement"
        | "with_statement"
        | "match_statement" => {
            facts.push(Fact::Unknown(Unknown {
                node: node_id(stmt),
                reason: format!("control flow not analyzed in Phase 1 ({})", stmt.kind()),
            }));
        }
        "ERROR" => {
            facts.push(Fact::Unknown(Unknown {
                node: node_id(stmt),
                reason: "parse error in statement".to_string(),
            }));
        }
        // Decorators, pass, break, continue, comments — no facts produced.
        _ => {}
    }
}

fn extract_assign_target(src: &[u8], lhs: Node<'_>, facts: &mut Vec<Fact>) {
    match lhs.kind() {
        "identifier" => {
            facts.push(Fact::Def(Def {
                node: node_id(lhs),
                symbol: node_text(src, lhs).to_string(),
                kind: DefKind::Assign,
            }));
        }
        "ERROR" => {
            facts.push(Fact::Unknown(Unknown {
                node: node_id(lhs),
                reason: "parse error in assignment target".to_string(),
            }));
        }
        // Tuple unpacking, attribute targets, subscript targets — Unknown for Phase 1.
        _ => {
            facts.push(Fact::Unknown(Unknown {
                node: node_id(lhs),
                reason: format!("unsupported assignment target ({})", lhs.kind()),
            }));
        }
    }
}

/// Recursively collect all identifier uses in an expression subtree.
/// Does NOT descend into nested function definitions (they have their own scope).
fn collect_uses(src: &[u8], node: Node<'_>, facts: &mut Vec<Fact>) {
    match node.kind() {
        "identifier" => {
            facts.push(Fact::Use(Use {
                node: node_id(node),
                symbol: node_text(src, node).to_string(),
            }));
        }
        // Don't collect name-parts of attribute access as variable uses.
        // `a.b` → only `a` is a local variable use; `b` is an attribute name.
        "attribute" => {
            if let Some(obj) = node.child_by_field_name("object") {
                collect_uses(src, obj, facts);
            }
            // Skip the `attribute` field — it's a name, not a variable reference.
        }
        // Literals produce no uses.
        "string" | "integer" | "float" | "true" | "false" | "none" | "ellipsis" => {}
        "ERROR" => {
            facts.push(Fact::Unknown(Unknown {
                node: node_id(node),
                reason: "parse error in expression".to_string(),
            }));
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_uses(src, child, facts);
            }
        }
    }
}
