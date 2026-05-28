use tree_sitter::Node;
use wake_engine::{Db, SourceFile};
use wake_schema::{
    BranchCondition, CallArgKind, Consumer, ConsumerKind, NarrowEffect, NullBranch, NullCallSite,
    NullDef, NullFact, NullFileFacts, NullFunctionFacts, NullLoop, NullReturn, NullabilityValue,
    RhsNullability,
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
        "if_statement" => extract_if_statement(src, stmt, facts),
        "while_statement" => extract_while_statement(src, stmt, facts),
        "for_statement" => extract_for_statement(src, stmt, facts),
        "assert_statement" => {
            // `assert COND[, msg]` — COND holds for all subsequent code.
            if let Some(cond) = stmt.named_child(0) {
                collect_consumers(src, cond, facts);
                facts.push(NullFact::Assume(extract_condition(src, cond)));
            }
        }
        // Not yet modeled — opaque barrier (precision-safe: clears reaching state,
        // so consumers inside are simply not analyzed; never a false positive).
        "try_statement" | "with_statement" | "match_statement" => {
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

// ── Control-flow extraction ───────────────────────────────────────────────────

/// Extract the facts of a block (the body of an `if`/`while`/`for`) as a list.
fn extract_block_facts(src: &[u8], block: Node<'_>) -> Vec<NullFact> {
    let mut facts = Vec::new();
    let mut cursor = block.walk();
    for stmt in block.children(&mut cursor) {
        extract_null_stmt(src, stmt, &mut facts);
    }
    facts
}

/// Find the body `block` of a compound statement / clause, robust to field-name
/// differences across grammar versions.
fn block_child<'a>(node: Node<'a>) -> Option<Node<'a>> {
    node.child_by_field_name("consequence")
        .or_else(|| node.child_by_field_name("body"))
        .or_else(|| {
            let mut cursor = node.walk();
            node.children(&mut cursor).find(|n| n.kind() == "block")
        })
}

fn extract_if_statement(src: &[u8], stmt: Node<'_>, facts: &mut Vec<NullFact>) {
    let cond = stmt.child_by_field_name("condition");
    // The condition is evaluated before either arm — its consumers belong in the
    // parent stream (analyzed with the pre-branch environment).
    if let Some(c) = cond {
        collect_consumers(src, c, facts);
    }
    let condition = cond
        .map(|c| extract_condition(src, c))
        .unwrap_or_else(BranchCondition::other);

    let then_arm = block_child(stmt).map(|b| extract_block_facts(src, b)).unwrap_or_default();

    let mut alternatives = Vec::new();
    let mut cursor = stmt.walk();
    for child in stmt.children(&mut cursor) {
        if matches!(child.kind(), "elif_clause" | "else_clause") {
            alternatives.push(child);
        }
    }
    let else_arm = build_else_chain(src, &alternatives);

    facts.push(NullFact::Branch(NullBranch {
        node: node_id(stmt),
        condition,
        then_arm,
        else_arm,
    }));
}

/// Build the else-arm fact list from a chain of `elif`/`else` clauses.
/// Each `elif` becomes a nested `Branch`; the terminal `else` becomes a body.
fn build_else_chain(src: &[u8], alts: &[Node<'_>]) -> Vec<NullFact> {
    let Some((first, rest)) = alts.split_first() else {
        return Vec::new();
    };
    match first.kind() {
        "else_clause" => {
            block_child(*first).map(|b| extract_block_facts(src, b)).unwrap_or_default()
        }
        "elif_clause" => {
            let mut arm = Vec::new();
            let cond = first.child_by_field_name("condition");
            if let Some(c) = cond {
                collect_consumers(src, c, &mut arm);
            }
            let condition = cond
                .map(|c| extract_condition(src, c))
                .unwrap_or_else(BranchCondition::other);
            let then_arm =
                block_child(*first).map(|b| extract_block_facts(src, b)).unwrap_or_default();
            let else_arm = build_else_chain(src, rest);
            arm.push(NullFact::Branch(NullBranch {
                node: node_id(*first),
                condition,
                then_arm,
                else_arm,
            }));
            arm
        }
        _ => Vec::new(),
    }
}

fn extract_while_statement(src: &[u8], stmt: Node<'_>, facts: &mut Vec<NullFact>) {
    let cond = stmt.child_by_field_name("condition");
    if let Some(c) = cond {
        collect_consumers(src, c, facts);
    }
    let condition = cond
        .map(|c| extract_condition(src, c))
        .unwrap_or_else(BranchCondition::other);
    let body = block_child(stmt).map(|b| extract_block_facts(src, b)).unwrap_or_default();
    facts.push(NullFact::Loop(NullLoop {
        node: node_id(stmt),
        condition,
        bound: Vec::new(),
        body,
    }));
}

fn extract_for_statement(src: &[u8], stmt: Node<'_>, facts: &mut Vec<NullFact>) {
    // The iterable is evaluated before the loop — its consumers go in the parent.
    let iterable = stmt.child_by_field_name("right");
    if let Some(it) = iterable {
        collect_consumers(src, it, facts);
    }
    // Iterating `None` raises TypeError, so a plain-variable iterable is provably
    // non-None inside (and after) the loop body. Narrow it to avoid a false
    // positive on a later use that is in fact unreachable when the iterable is None.
    let condition = match iterable {
        Some(it) if it.kind() == "identifier" => BranchCondition {
            symbol: Some(node_text(src, it).to_string()),
            on_true: NarrowEffect::NonNull,
            on_false: NarrowEffect::Keep,
            opaque_refs: Vec::new(),
        },
        _ => BranchCondition::other(),
    };
    let mut bound = Vec::new();
    if let Some(target) = stmt.child_by_field_name("left") {
        collect_identifiers(src, target, &mut bound);
    }
    let body = block_child(stmt).map(|b| extract_block_facts(src, b)).unwrap_or_default();
    facts.push(NullFact::Loop(NullLoop {
        node: node_id(stmt),
        condition,
        bound,
        body,
    }));
}

/// Map a Python condition expression onto its language-neutral narrowing effect.
fn extract_condition(src: &[u8], cond: Node<'_>) -> BranchCondition {
    match cond.kind() {
        // `if x:` — truthy ⇒ NonNull; falsy side tells us nothing definite.
        "identifier" => BranchCondition {
            symbol: Some(node_text(src, cond).to_string()),
            on_true: NarrowEffect::NonNull,
            on_false: NarrowEffect::Keep,
            opaque_refs: Vec::new(),
        },
        "parenthesized_expression" => cond
            .named_child(0)
            .map(|inner| extract_condition(src, inner))
            .unwrap_or_else(BranchCondition::other),
        "not_operator" => match cond.child_by_field_name("argument") {
            // `if not x:` — true side is falsy (not necessarily None), false side NonNull.
            Some(a) if a.kind() == "identifier" => BranchCondition {
                symbol: Some(node_text(src, a).to_string()),
                on_true: NarrowEffect::Unknown,
                on_false: NarrowEffect::NonNull,
                opaque_refs: Vec::new(),
            },
            _ => opaque_condition(src, cond),
        },
        "comparison_operator" => parse_none_comparison(src, cond),
        _ => opaque_condition(src, cond),
    }
}

/// A condition we cannot interpret: every variable it references is set Unknown
/// on both sides so an unmodeled guard never yields a false positive.
fn opaque_condition(src: &[u8], cond: Node<'_>) -> BranchCondition {
    let mut refs = Vec::new();
    collect_identifiers(src, cond, &mut refs);
    BranchCondition {
        symbol: None,
        on_true: NarrowEffect::Keep,
        on_false: NarrowEffect::Keep,
        opaque_refs: refs,
    }
}

/// Recognize `x is None`, `x is not None`, `x == None`, `x != None` (either order).
/// tree-sitter-python tokenizes `is not` as a single anonymous `"is not"` token.
fn parse_none_comparison(src: &[u8], cond: Node<'_>) -> BranchCondition {
    let mut ident: Option<String> = None;
    let mut has_none = false;
    let mut op: Option<&str> = None;
    let mut other_operand = false;

    let mut cursor = cond.walk();
    for child in cond.children(&mut cursor) {
        match child.kind() {
            "identifier" => {
                if ident.is_none() {
                    ident = Some(node_text(src, child).to_string());
                } else {
                    other_operand = true;
                }
            }
            "none" => has_none = true,
            "is" | "is not" | "==" | "!=" => op = Some(child.kind()),
            // Any other named operand (literal, call, attribute, second comparison)
            // means this is not a simple `<var> <op> None` test.
            other => {
                if child.is_named() && other != "comment" {
                    other_operand = true;
                }
            }
        }
    }

    if other_operand || !has_none {
        return opaque_condition(src, cond);
    }
    let (Some(sym), Some(o)) = (ident, op) else {
        return opaque_condition(src, cond);
    };

    // Does the test evaluate true when the variable *is* None?
    let true_when_none = match o {
        "is" | "==" => true,
        "is not" | "!=" => false,
        _ => return opaque_condition(src, cond),
    };
    if true_when_none {
        BranchCondition {
            symbol: Some(sym),
            on_true: NarrowEffect::Nullable,
            on_false: NarrowEffect::NonNull,
            opaque_refs: Vec::new(),
        }
    } else {
        BranchCondition {
            symbol: Some(sym),
            on_true: NarrowEffect::NonNull,
            on_false: NarrowEffect::Nullable,
            opaque_refs: Vec::new(),
        }
    }
}

/// Collect local-variable identifier names referenced in an expression
/// (the object of an attribute access, not the attribute name).
fn collect_identifiers(src: &[u8], node: Node<'_>, out: &mut Vec<String>) {
    match node.kind() {
        "identifier" => out.push(node_text(src, node).to_string()),
        "attribute" => {
            if let Some(obj) = node.child_by_field_name("object") {
                collect_identifiers(src, obj, out);
            }
        }
        _ => {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                collect_identifiers(src, child, out);
            }
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
///
/// Token-aware: a type whose *name* merely contains the substring "None"
/// (e.g. `NoneCheck`, `MyNoneType`) is not treated as nullable. We look for
/// `None`/`NoneType` as standalone identifier tokens.
pub fn parse_annotation_text(text: &str) -> NullabilityValue {
    let t = text.trim();

    // The bare None type (and its runtime spelling NoneType).
    if t == "None" || t == "NoneType" {
        return NullabilityValue::Nullable;
    }
    // `Optional[...]` is `Union[..., None]` — always nullable.
    if head_name(t) == "Optional" && t.contains('[') {
        return NullabilityValue::Nullable;
    }
    // `Union[..., None, ...]` or a PEP 604 `X | None` union that includes None.
    let is_union = (head_name(t) == "Union" && t.contains('[')) || t.contains('|');
    if is_union && mentions_none_token(t) {
        return NullabilityValue::Nullable;
    }
    if is_known_non_nullable(t) {
        return NullabilityValue::NonNull;
    }

    NullabilityValue::Unknown
}

/// The unqualified head of an annotation: the identifier before the first
/// subscript/union, stripped of any module qualifier (`typing.Optional` →
/// `Optional`).
fn head_name(t: &str) -> &str {
    let head = t.split(['[', '|']).next().unwrap_or(t).trim();
    head.rsplit('.').next().unwrap_or(head)
}

/// True if `None` or `NoneType` appears as a standalone identifier token.
fn mentions_none_token(t: &str) -> bool {
    let bytes = t.as_bytes();
    let is_ident = |b: u8| b == b'_' || b.is_ascii_alphanumeric();
    let mut start = 0;
    while start < bytes.len() {
        if !is_ident(bytes[start]) {
            start += 1;
            continue;
        }
        let mut end = start;
        while end < bytes.len() && is_ident(bytes[end]) {
            end += 1;
        }
        let tok = &t[start..end];
        if tok == "None" || tok == "NoneType" {
            return true;
        }
        start = end;
    }
    false
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
        // A container/callable value is itself non-None regardless of its element
        // types, but we keep the original conservative stance and only assert
        // NonNull when None is not mentioned as a standalone token.
        prefixes.iter().any(|p| t.starts_with(p)) && !mentions_none_token(t)
    }
}
