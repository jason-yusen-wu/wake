use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_engine::{Db, SourceFile};
use wake_prop_null::null_regressions;
use wake_schema::{ConsumerKind, NullRegression};

// ── Test database ─────────────────────────────────────────────────────────────

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

fn regressions_for(src: &str) -> Vec<NullRegression> {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    let all = null_regressions(&db, file);
    all.into_iter().flat_map(|(_, regs)| regs).collect()
}

fn count_regressions(src: &str) -> usize {
    regressions_for(src).len()
}

fn has_regression_for(src: &str, symbol: &str) -> bool {
    regressions_for(src).iter().any(|r| r.object_symbol == symbol)
}

// ── 1. No false positives: unannotated parameters ─────────────────────────────

/// Unannotated params are Unknown — must not trigger regressions.
#[test]
fn unannotated_param_no_regression() {
    let src = "def f(x):\n    return x.attr\n";
    assert_eq!(count_regressions(src), 0, "Unknown param must not trigger regression");
}

// ── 2. NonNull annotation: no regression ─────────────────────────────────────

#[test]
fn str_annotated_param_no_regression() {
    let src = "def f(x: str):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0, "str param is NonNull, no regression");
}

#[test]
fn int_annotated_param_no_regression() {
    let src = "def f(x: int):\n    y = x + 1\n    return y\n";
    assert_eq!(count_regressions(src), 0, "int param is NonNull, no regression");
}

// ── 3. Nullable annotation: regression at consumer ───────────────────────────

#[test]
fn optional_param_attribute_regression() {
    let src = "def f(x: Optional[str]):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1, "Optional param consumed via attribute → 1 regression");
    assert!(has_regression_for(src, "x"));
}

#[test]
fn optional_param_subscript_regression() {
    let src = "def f(x: Optional[list]):\n    return x[0]\n";
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 1);
    assert_eq!(regs[0].object_symbol, "x");
    assert_eq!(regs[0].kind, ConsumerKind::Subscript);
}

#[test]
fn optional_param_call_regression() {
    let src = "def f(x: Optional[object]):\n    return x()\n";
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 1);
    assert_eq!(regs[0].object_symbol, "x");
    assert_eq!(regs[0].kind, ConsumerKind::Call);
}

// ── 4. Union[T, None] annotation ─────────────────────────────────────────────

#[test]
fn union_none_param_regression() {
    let src = "def f(x: Union[str, None]):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 5. PEP 604 T | None annotation ───────────────────────────────────────────

#[test]
fn pep604_pipe_none_regression() {
    let src = "def f(x: str | None):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

#[test]
fn pep604_none_pipe_regression() {
    let src = "def f(x: None | str):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 6. Assignment: x = None → Nullable ───────────────────────────────────────

#[test]
fn assign_none_then_attribute_regression() {
    let src = "def f():\n    x = None\n    return x.attr\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

#[test]
fn assign_none_then_subscript_regression() {
    let src = "def f():\n    x = None\n    return x[0]\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

#[test]
fn assign_none_then_call_regression() {
    let src = "def f():\n    x = None\n    return x()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 7. Assignment: x = literal → NonNull ─────────────────────────────────────

#[test]
fn assign_string_literal_no_regression() {
    let src = "def f():\n    x = \"hello\"\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0, "string literal is NonNull");
}

#[test]
fn assign_int_literal_no_regression() {
    let src = "def f():\n    x = 42\n    return x.bit_length()\n";
    assert_eq!(count_regressions(src), 0, "int literal is NonNull");
}

#[test]
fn assign_list_literal_no_regression() {
    let src = "def f():\n    x = [1, 2, 3]\n    return x[0]\n";
    assert_eq!(count_regressions(src), 0, "list literal is NonNull");
}

// ── 8. Variable copy: x = y propagates nullability ───────────────────────────

#[test]
fn nullable_copy_propagates_regression() {
    let src = "def f(y: Optional[str]):\n    x = y\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"), "x copied from Nullable y → regression");
}

#[test]
fn nonnull_copy_no_regression() {
    let src = "def f(y: str):\n    x = y\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0, "x copied from NonNull y → no regression");
}

// ── 9. Reassignment clears previous nullability ───────────────────────────────

#[test]
fn reassign_nonnull_clears_nullable() {
    let src = "def f():\n    x = None\n    x = \"hello\"\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0, "x reassigned to NonNull → no regression");
}

#[test]
fn reassign_none_after_nonnull_triggers_regression() {
    let src = "def f():\n    x = \"hello\"\n    x = None\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 10. Annotated assignment: annotation takes precedence over RHS ────────────

#[test]
fn annotated_assign_nullable_overrides_nonnull_rhs() {
    // x: Optional[str] = "hello" — annotation says Nullable even though RHS is NonNull
    let src = "def f():\n    x: Optional[str] = \"hello\"\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1, "annotation Nullable wins over NonNull RHS");
}

#[test]
fn annotated_assign_nonnull_overrides_none_rhs() {
    // x: str = None — annotation says NonNull; we trust the annotation
    let src = "def f():\n    x: str = None\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0, "annotation NonNull wins over None RHS");
}

// ── 11. Multiple consumers of the same Nullable variable ─────────────────────

#[test]
fn multiple_consumers_multiple_regressions() {
    let src = "def f(x: Optional[str]):\n    a = x.upper()\n    b = x.lower()\n    return b\n";
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 2, "two consumer sites → two regressions");
    assert!(regs.iter().all(|r| r.object_symbol == "x"));
}

// ── 12. Consumer on NonNull is not a regression ───────────────────────────────

#[test]
fn consumer_on_nonnull_no_regression() {
    let src = "def f(x: str):\n    a = x.upper()\n    b = x.lower()\n    return b\n";
    assert_eq!(count_regressions(src), 0);
}

// ── 13. Nested attribute: only innermost identifier matters ───────────────────

/// x.y.z — x is the local variable we can reason about.
/// If x is Nullable, accessing x.y is a regression; x.y.z means x is consumed at x.y.
#[test]
fn chained_attribute_regression_on_innermost() {
    let src = "def f(x: Optional[object]):\n    return x.y.z\n";
    // x.y.z → attribute(attribute(x, y), z)
    // innermost: attribute(x, y) → Consumer(x, Attribute) → regression
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 14. Control-flow join: state after a branch is conservatively Unknown ─────
// (Branch/loop analysis with guard narrowing has dedicated coverage in
// tests/control_flow.rs; these assert the post-merge no-false-positive property.)

#[test]
fn use_after_if_is_not_a_false_positive() {
    // `if x: pass` narrows x to NonNull on the true side; the merge with the
    // (still-Nullable) fall-through is Unknown, so the later use does not fire.
    let src = "def f(x: Optional[str]):\n    if x:\n        pass\n    return x.upper()\n";
    assert_eq!(
        count_regressions(src),
        0,
        "post-branch merge is Unknown, not Nullable — no false positive"
    );
}

#[test]
fn use_after_for_over_optional_is_not_a_false_positive() {
    // Iterating x proves it non-None, so the later subscript is not reachable
    // with x == None — must not be reported.
    let src = "def f(x: Optional[list]):\n    for i in x:\n        pass\n    return x[0]\n";
    assert_eq!(count_regressions(src), 0, "for-loop over x narrows it; no false positive");
}

#[test]
fn use_after_while_guard_is_not_a_false_positive() {
    let src = "def f(x: Optional[object]):\n    while x:\n        pass\n    return x()\n";
    assert_eq!(count_regressions(src), 0, "while-guard narrows x; post-merge Unknown");
}

// ── 15. Consumer before a branch still fires ──────────────────────────────────

#[test]
fn consumer_before_branch_fires() {
    let src = "def f(x: Optional[str]):\n    y = x.upper()\n    if y:\n        pass\n    return y\n";
    // x.upper() before the branch → straight-line regression on x.
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 16. Parse error in function body: Unknown barrier, no panic ───────────────

#[test]
fn parse_error_no_panic_no_regression() {
    // Syntactically broken; tree-sitter ERROR node → Unknown barrier.
    let src = "def f(x: Optional[str]):\n    z x\n    return x.upper()\n";
    // After the ERROR barrier, x is Unknown → no regression.
    // Before the barrier, x.upper() is not present.
    let result = std::panic::catch_unwind(|| count_regressions(src));
    assert!(result.is_ok(), "must not panic on parse errors");
    assert_eq!(result.unwrap(), 0, "after ERROR barrier, state cleared — no regression");
}

// ── 17. Multiple functions in one file: independent analysis ──────────────────

#[test]
fn multiple_functions_independent() {
    let src = concat!(
        "def f(x: Optional[str]):\n",
        "    return x.upper()\n",
        "def g(y: str):\n",
        "    return y.lower()\n",
    );
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    let all = null_regressions(&db, file);
    assert_eq!(all.len(), 2, "two functions");

    let (_, regs_f) = &all[0];
    let (_, regs_g) = &all[1];
    assert_eq!(regs_f.len(), 1, "f: Optional param consumed → regression");
    assert_eq!(regs_g.len(), 0, "g: NonNull param → no regression");
}

// ── 18. Empty function: no regressions ───────────────────────────────────────

#[test]
fn empty_function_no_regressions() {
    let src = "def f():\n    pass\n";
    assert_eq!(count_regressions(src), 0);
}

// ── 19. Function with only a return: no regressions ──────────────────────────

#[test]
fn return_none_literal_no_regression() {
    let src = "def f():\n    return None\n";
    assert_eq!(count_regressions(src), 0);
}

// ── 20. Default parameters are Unknown: no regression ────────────────────────

#[test]
fn default_parameter_unknown_no_regression() {
    let src = "def f(x=None):\n    return x.attr\n";
    // default_parameter → Unknown annotation → Unknown at consumer → no regression
    assert_eq!(count_regressions(src), 0);
}

// ── 21. Typed default parameter: annotation governs ──────────────────────────

#[test]
fn typed_default_nullable_regression() {
    let src = "def f(x: Optional[str] = None):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

#[test]
fn typed_default_nonnull_no_regression() {
    let src = "def f(x: str = \"hi\"):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 0);
}

// ── 22. *args and **kwargs: Unknown — no regression ──────────────────────────

#[test]
fn star_args_no_regression() {
    let src = "def f(*args):\n    return args[0]\n";
    assert_eq!(count_regressions(src), 0, "*args is Unknown, subscript not Nullable");
}

// ── 23. Augmented assignment: x += expr → x becomes Unknown ─────────────────

#[test]
fn augmented_assign_makes_unknown_no_regression() {
    // After x += expr, x is reassigned to Unknown (augmented assign in extractor)
    let src = "def f(x: Optional[str]):\n    x += \"suffix\"\n    return x.upper()\n";
    // augmented_assignment: x is consumed (collect_consumers on rhs is optional here),
    // then x is redefined as Unknown → consumer after sees Unknown → no regression
    assert_eq!(count_regressions(src), 0, "augmented assign sets x to Unknown");
}

// ── 24. Incrementality: editing file_b does not re-execute null_regressions(file_a) ──

#[test]
fn incrementality_independent_files() {
    let (mut db, executions) = tracking_db();

    let src_a = "def foo(x: Optional[str]):\n    return x.upper()\n";
    let src_b = "def bar(y: str):\n    return y.lower()\n";

    let file_a = SourceFile::new(&db, src_a.to_string());
    let file_b = SourceFile::new(&db, src_b.to_string());

    // Warm both caches.
    let regs_a = null_regressions(&db, file_a);
    let _regs_b = null_regressions(&db, file_b);

    // Modify file_b.
    file_b.set_contents(&mut db).to("def bar(y: Optional[str]):\n    return y.lower()\n".to_string());

    // Querying file_a must return the cached result without re-executing.
    *executions.lock().unwrap() = 0;
    let regs_a2 = null_regressions(&db, file_a);
    assert_eq!(regs_a, regs_a2, "file_a result unchanged");
    assert_eq!(
        *executions.lock().unwrap(),
        0,
        "null_regressions(file_a) must not re-execute when file_b changes"
    );

    // Querying file_b must re-execute.
    *executions.lock().unwrap() = 0;
    let regs_b2 = null_regressions(&db, file_b);
    assert!(
        *executions.lock().unwrap() > 0,
        "null_regressions(file_b) must re-execute after source change"
    );
    // bar now has an Optional param consumed → 1 regression
    let b_regs: Vec<_> = regs_b2.into_iter().flat_map(|(_, r)| r).collect();
    assert_eq!(b_regs.len(), 1, "after edit, bar has 1 regression");
}

// ── 25. Regression struct fields are correct ──────────────────────────────────

#[test]
fn regression_fields_correct() {
    let src = "def f(x: Optional[str]):\n    return x.upper()\n";
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 1);
    let r = &regs[0];
    assert_eq!(r.object_symbol, "x");
    assert_eq!(r.kind, ConsumerKind::Attribute);
    // func_node and consumer_node are byte-range NodeIds — just check they're non-zero
    assert!(r.func_node.start_byte < r.func_node.end_byte);
    assert!(r.consumer_node.start_byte < r.consumer_node.end_byte);
}

// ── 26. Variable only consumed, never defined: Unknown (outer scope) ──────────

#[test]
fn undeclared_variable_no_regression() {
    // `result` is never defined in this function — it comes from outer scope (Unknown).
    let src = "def f():\n    return result.attr\n";
    assert_eq!(count_regressions(src), 0, "outer-scope variable is Unknown, not Nullable");
}

// ── 27. Chained call: x.upper().lower() ───────────────────────────────────────

#[test]
fn chained_call_regression_on_x() {
    let src = "def f(x: Optional[str]):\n    return x.upper().lower()\n";
    // x.upper() → Consumer(x, Attribute) → regression
    // x.upper().lower() → Consumer(x.upper(), Attribute) → x.upper() is not a local var → no extra consumer
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 28. Consumer in function arguments ───────────────────────────────────────

#[test]
fn consumer_in_call_args_regression() {
    let src = "def f(x: Optional[list]):\n    print(x[0])\n";
    // x[0] is inside the arguments of print(...) — collect_consumers recurses into args
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 29. None annotation (unusual but valid) ───────────────────────────────────

#[test]
fn none_annotation_is_nullable() {
    let src = "def f(x: None):\n    return x.attr\n";
    assert_eq!(count_regressions(src), 1, "None annotation → Nullable → regression");
}

// ── 30. List[str] annotation: NonNull, no regression ─────────────────────────

#[test]
fn list_str_annotation_no_regression() {
    let src = "def f(x: List[str]):\n    return x[0]\n";
    assert_eq!(count_regressions(src), 0, "List[str] is NonNull");
}
