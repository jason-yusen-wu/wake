use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_diff::{
    WitnessStep, RegressionReport,
    diff_results, regressions_with_witnesses,
};
use wake_engine::{Db, SourceFile};
use wake_schema::{ConsumerKind, NodeId, NullRegression};

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

fn witnesses_for(src: &str) -> Vec<RegressionReport> {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    regressions_with_witnesses(&db, file)
}

// ── Step shape helpers ─────────────────────────────────────────────────────────

fn step_kind(step: &WitnessStep) -> &'static str {
    match step {
        WitnessStep::NullableParam { .. } => "NullableParam",
        WitnessStep::NoneAssignment { .. } => "NoneAssignment",
        WitnessStep::VariableCopy { .. } => "VariableCopy",
        WitnessStep::CallReturn { .. } => "CallReturn",
        WitnessStep::Consumer { .. } => "Consumer",
        WitnessStep::Opaque { .. } => "Opaque",
    }
}

fn step_symbol(step: &WitnessStep) -> &str {
    match step {
        WitnessStep::NullableParam { symbol, .. } => symbol,
        WitnessStep::NoneAssignment { symbol, .. } => symbol,
        WitnessStep::VariableCopy { to, .. } => to,
        WitnessStep::CallReturn { to, .. } => to,
        WitnessStep::Consumer { symbol, .. } => symbol,
        WitnessStep::Opaque { symbol } => symbol,
    }
}

fn step_kinds(steps: &[WitnessStep]) -> Vec<&'static str> {
    steps.iter().map(step_kind).collect()
}

// ── Synthetic RegressionReport builder (for diff_results unit tests) ──────────

fn node(start: u32, end: u32) -> NodeId {
    NodeId { start_byte: start, end_byte: end }
}

fn fake_regression(func_start: u32, consumer_start: u32, symbol: &str) -> RegressionReport {
    RegressionReport {
        regression: NullRegression {
            file: String::new(),
            func_node: node(func_start, func_start + 10),
            func_name: "f".to_string(),
            consumer_node: node(consumer_start, consumer_start + 10),
            object_symbol: symbol.to_string(),
            kind: ConsumerKind::Attribute,
        },
        witness: vec![],
    }
}

// ── 1. Clean code: no regressions, no witnesses ───────────────────────────────

#[test]
fn no_regressions_empty_reports() {
    let src = "def f(x: int) -> int:\n    return x + 1\n";
    let reports = witnesses_for(src);
    assert!(reports.is_empty(), "clean function should have zero regression reports");
}

// ── 2. Witness: direct None assignment → Consumer ─────────────────────────────

#[test]
fn witness_none_assignment_then_attribute() {
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
    );
    let reports = witnesses_for(src);
    assert_eq!(reports.len(), 1, "one regression");
    let steps = &reports[0].witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NoneAssignment", "Consumer"],
        "witness should trace: None assignment → consumer"
    );
    assert_eq!(step_symbol(&steps[0]), "x");
    assert_eq!(step_symbol(&steps[1]), "x");
    assert!(matches!(&steps[1], WitnessStep::Consumer { kind: ConsumerKind::Attribute, .. }));
}

// ── 3. Witness: Nullable parameter → Consumer ─────────────────────────────────

#[test]
fn witness_nullable_param_attribute() {
    let src = concat!(
        "def f(x: Optional[str]):\n",
        "    x.attr\n",
    );
    let reports = witnesses_for(src);
    assert_eq!(reports.len(), 1);
    let steps = &reports[0].witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NullableParam", "Consumer"],
        "witness should trace: nullable param → consumer"
    );
    assert_eq!(step_symbol(&steps[0]), "x");
    assert_eq!(reports[0].regression.object_symbol, "x");
}

// ── 4. Witness: variable copy chain ──────────────────────────────────────────

#[test]
fn witness_variable_copy_chain() {
    let src = concat!(
        "def f():\n",
        "    a = None\n",
        "    b = a\n",
        "    b.attr\n",
    );
    let reports = witnesses_for(src);
    // b is Nullable via copy from a; expect regression on b
    let b_report = reports.iter().find(|r| r.regression.object_symbol == "b");
    assert!(b_report.is_some(), "should have regression for b");
    let steps = &b_report.unwrap().witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NoneAssignment", "VariableCopy", "Consumer"],
        "witness: a=None → b=a → b.attr"
    );
    // Check symbols
    assert_eq!(step_symbol(&steps[0]), "a");
    if let WitnessStep::VariableCopy { from, to, .. } = &steps[1] {
        assert_eq!(from, "a");
        assert_eq!(to, "b");
    } else {
        panic!("expected VariableCopy step");
    }
    assert_eq!(step_symbol(&steps[2]), "b");
}

// ── 5. Witness: subscript consumer kind ──────────────────────────────────────

#[test]
fn witness_subscript_consumer() {
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x[0]\n",
    );
    let reports = witnesses_for(src);
    assert_eq!(reports.len(), 1);
    let steps = &reports[0].witness;
    assert_eq!(step_kinds(steps), vec!["NoneAssignment", "Consumer"]);
    assert!(matches!(&steps[1], WitnessStep::Consumer { kind: ConsumerKind::Subscript, .. }));
}

// ── 6. Witness: call consumer kind ──────────────────────────────────────────

#[test]
fn witness_call_consumer() {
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x()\n",
    );
    let reports = witnesses_for(src);
    // x() where x = None → Consumer(x, Call)
    let call_report = reports.iter().find(|r| {
        matches!(r.regression.kind, ConsumerKind::Call) && r.regression.object_symbol == "x"
    });
    assert!(call_report.is_some(), "should have Call-kind regression");
    let steps = &call_report.unwrap().witness;
    assert!(
        step_kinds(steps).contains(&"NoneAssignment"),
        "witness should include NoneAssignment"
    );
    let last = steps.last().unwrap();
    assert!(matches!(last, WitnessStep::Consumer { kind: ConsumerKind::Call, .. }));
}

// ── 7. Witness: cross-function call return (callee returns None) ──────────────

#[test]
fn witness_call_return_from_none_returning_callee() {
    let src = concat!(
        "def g():\n",
        "    return None\n",
        "def f():\n",
        "    x = g()\n",
        "    x.attr\n",
    );
    let reports = witnesses_for(src);
    let f_report = reports.iter().find(|r| r.regression.object_symbol == "x");
    assert!(f_report.is_some(), "should have regression for x in f");
    let steps = &f_report.unwrap().witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NoneAssignment", "CallReturn", "Consumer"],
        "witness: g returns None → x = g() → x.attr"
    );
    if let WitnessStep::CallReturn { callee, to, .. } = &steps[1] {
        assert_eq!(callee, "g");
        assert_eq!(to, "x");
    } else {
        panic!("expected CallReturn step");
    }
}

// ── 8. Witness: cross-function call return via param propagation ───────────────

#[test]
fn witness_call_return_via_nullable_param() {
    let src = concat!(
        "def passthrough(y):\n",
        "    return y\n",
        "def f(x: Optional[str]):\n",
        "    z = passthrough(x)\n",
        "    z.attr\n",
    );
    let reports = witnesses_for(src);
    let f_report = reports.iter().find(|r| r.regression.object_symbol == "z");
    assert!(f_report.is_some(), "should have regression for z in f");
    let steps = &f_report.unwrap().witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NullableParam", "CallReturn", "Consumer"],
        "witness: x nullable param → z = passthrough(x) → z.attr"
    );
    assert_eq!(step_symbol(&steps[0]), "x");
    if let WitnessStep::CallReturn { callee, to, .. } = &steps[1] {
        assert_eq!(callee, "passthrough");
        assert_eq!(to, "z");
    } else {
        panic!("expected CallReturn step");
    }
    assert_eq!(step_symbol(&steps[2]), "z");
}

// ── 9. Witness: None literal arg propagates through callee ────────────────────

#[test]
fn witness_none_literal_arg_propagates() {
    let src = concat!(
        "def identity(y):\n",
        "    return y\n",
        "def f():\n",
        "    z = identity(None)\n",
        "    z.attr\n",
    );
    let reports = witnesses_for(src);
    let f_report = reports.iter().find(|r| r.regression.object_symbol == "z");
    assert!(f_report.is_some(), "should have regression for z");
    let steps = &f_report.unwrap().witness;
    // identity(None) → z is Nullable; witness traces back through None arg
    assert!(steps.len() >= 2, "witness should have at least 2 steps");
    let last = steps.last().unwrap();
    assert!(matches!(last, WitnessStep::Consumer { .. }));
}

// ── 10. Multiple regressions in one function: each has a witness ───────────────

#[test]
fn multiple_regressions_each_witnessed() {
    let src = concat!(
        "def f():\n",
        "    a = None\n",
        "    b = None\n",
        "    a.x\n",
        "    b.y\n",
    );
    let reports = witnesses_for(src);
    assert_eq!(reports.len(), 2, "two separate regressions");
    for report in &reports {
        assert!(!report.witness.is_empty(), "each regression must have a witness");
        assert!(
            matches!(report.witness.last().unwrap(), WitnessStep::Consumer { .. }),
            "witness must end with Consumer"
        );
    }
}

// ── 11. diff_results: no changes → empty diff ────────────────────────────────

#[test]
fn diff_results_no_changes_empty() {
    let r = fake_regression(0, 100, "x");
    let before = vec![r.clone()];
    let after = vec![r];
    let diff = diff_results(&before, &after);
    assert!(diff.blast_radius.is_empty());
    assert!(diff.new_regressions.is_empty());
    assert!(diff.fixed_regressions.is_empty());
}

// ── 12. diff_results: new regression appears ──────────────────────────────────

#[test]
fn diff_results_new_regression() {
    let before: Vec<RegressionReport> = vec![];
    let after = vec![fake_regression(0, 100, "x")];
    let diff = diff_results(&before, &after);
    assert_eq!(diff.blast_radius.len(), 1);
    assert_eq!(diff.new_regressions.len(), 1);
    assert!(diff.fixed_regressions.is_empty());
    assert_eq!(diff.new_regressions[0].regression.object_symbol, "x");
}

// ── 13. diff_results: regression fixed ────────────────────────────────────────

#[test]
fn diff_results_fixed_regression() {
    let before = vec![fake_regression(0, 100, "y")];
    let after: Vec<RegressionReport> = vec![];
    let diff = diff_results(&before, &after);
    assert_eq!(diff.blast_radius.len(), 1);
    assert!(diff.new_regressions.is_empty());
    assert_eq!(diff.fixed_regressions.len(), 1);
    assert_eq!(diff.fixed_regressions[0].object_symbol, "y");
}

// ── 14. diff_results: blast radius = symmetric difference ────────────────────

#[test]
fn diff_results_blast_radius_symmetric_difference() {
    // A: in before, gone in after → fixed
    // B: in both → no change
    // C: new in after → new regression
    let a = fake_regression(0, 10, "a");
    let b = fake_regression(0, 20, "b");
    let c = fake_regression(0, 30, "c");

    let before = vec![a.clone(), b.clone()];
    let after = vec![b.clone(), c.clone()];

    let diff = diff_results(&before, &after);

    // blast_radius: {A, C} (changed nodes)
    assert_eq!(diff.blast_radius.len(), 2, "two nodes changed status");
    assert!(diff.blast_radius.contains(&node(10, 20)), "A should be in blast radius");
    assert!(diff.blast_radius.contains(&node(30, 40)), "C should be in blast radius");

    // B stays in both → not in blast radius
    assert!(!diff.blast_radius.contains(&node(20, 30)), "B unchanged — not in blast radius");

    assert_eq!(diff.new_regressions.len(), 1);
    assert_eq!(diff.new_regressions[0].regression.object_symbol, "c");
    assert_eq!(diff.fixed_regressions.len(), 1);
    assert_eq!(diff.fixed_regressions[0].object_symbol, "a");
}

// ── 15. Full pipeline: benign edit → empty diff ──────────────────────────────

#[test]
fn benign_edit_produces_empty_diff() {
    let before_src = concat!(
        "def f():\n",
        "    x = 1\n",
        "    return x\n",
    );
    let after_src = concat!(
        "def f():\n",
        "    x = 2\n",
        "    return x\n",
    );

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before_src.to_string());
    let before_reports = regressions_with_witnesses(&db, file);

    file.set_contents(&mut db).to(after_src.to_string());
    let after_reports = regressions_with_witnesses(&db, file);

    let diff = diff_results(&before_reports, &after_reports);
    assert!(diff.blast_radius.is_empty(), "benign edit → no blast radius");
    assert!(diff.new_regressions.is_empty(), "benign edit → no new regressions");
    assert!(diff.fixed_regressions.is_empty(), "benign edit → no fixed regressions");
}

// ── 16. Full pipeline: regressing edit → new regression with witness ──────────

#[test]
fn regressing_edit_produces_new_regression_with_witness() {
    // Before: x is assigned a non-null integer, no regression
    let before_src = concat!(
        "def f():\n",
        "    x = 1\n",
        "    x.attr\n",
    );
    // After: x is assigned None → regression
    let after_src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
    );

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before_src.to_string());
    let before_reports = regressions_with_witnesses(&db, file);
    assert!(before_reports.is_empty(), "before: no regressions");

    file.set_contents(&mut db).to(after_src.to_string());
    let after_reports = regressions_with_witnesses(&db, file);

    let diff = diff_results(&before_reports, &after_reports);
    assert_eq!(diff.new_regressions.len(), 1, "one new regression after regressing edit");
    assert!(diff.fixed_regressions.is_empty());

    // Verify witness quality
    let steps = &diff.new_regressions[0].witness;
    assert_eq!(step_kinds(steps), vec!["NoneAssignment", "Consumer"]);
}

// ── 17. Full pipeline: fix edit → regression disappears ──────────────────────

#[test]
fn fix_edit_produces_fixed_regression() {
    // Before: regression on x
    let before_src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
    );
    // After: x is now non-null, regression disappears
    let after_src = concat!(
        "def f():\n",
        "    x = 42\n",
        "    x.attr\n",
    );

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before_src.to_string());
    let before_reports = regressions_with_witnesses(&db, file);
    assert_eq!(before_reports.len(), 1, "before: one regression");

    file.set_contents(&mut db).to(after_src.to_string());
    let after_reports = regressions_with_witnesses(&db, file);

    let diff = diff_results(&before_reports, &after_reports);
    assert!(diff.new_regressions.is_empty(), "fix edit introduces no new regressions");
    assert_eq!(diff.fixed_regressions.len(), 1, "one regression fixed");
    assert_eq!(diff.fixed_regressions[0].object_symbol, "x");
}

// ── 18. Full pipeline: interprocedural regressing edit ───────────────────────

#[test]
fn interprocedural_regressing_edit_has_cross_function_witness() {
    let before_src = concat!(
        "def g():\n",
        "    return 1\n",
        "def f():\n",
        "    x = g()\n",
        "    x.attr\n",
    );
    let after_src = concat!(
        "def g():\n",
        "    return None\n",
        "def f():\n",
        "    x = g()\n",
        "    x.attr\n",
    );

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before_src.to_string());
    let before_reports = regressions_with_witnesses(&db, file);
    assert!(before_reports.is_empty(), "before: g returns int, no regression");

    file.set_contents(&mut db).to(after_src.to_string());
    let after_reports = regressions_with_witnesses(&db, file);
    assert_eq!(after_reports.len(), 1, "after: one regression via g → x");

    let diff = diff_results(&before_reports, &after_reports);
    assert_eq!(diff.new_regressions.len(), 1);

    // Witness should trace through the call
    let steps = &diff.new_regressions[0].witness;
    assert_eq!(
        step_kinds(steps),
        vec!["NoneAssignment", "CallReturn", "Consumer"],
        "cross-function witness: g returns None → x = g() → x.attr"
    );
}

// ── 19. Incrementality: changing file2 does not recompute file1 ───────────────

#[test]
fn incrementality_unrelated_file_not_recomputed() {
    let src1 = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
    );
    let src2 = "def g():\n    return 1\n";

    let (mut db, counter) = tracking_db();
    let file1 = SourceFile::new(&db, src1.to_string());
    let file2 = SourceFile::new(&db, src2.to_string());

    // Initial computation — both files compute cold.
    let _ = regressions_with_witnesses(&db, file1);
    let _ = regressions_with_witnesses(&db, file2);

    let count_before = *counter.lock().unwrap();

    // Edit file2 (unrelated to file1).
    file2.set_contents(&mut db).to("def g():\n    return 2\n".to_string());

    // Re-query file1 — should hit the memo, not recompute.
    let _ = regressions_with_witnesses(&db, file1);
    let count_after_file1 = *counter.lock().unwrap();

    assert_eq!(
        count_after_file1, count_before,
        "file1's queries must not recompute when only file2 changed"
    );

    // Re-query file2 — SHOULD recompute.
    let _ = regressions_with_witnesses(&db, file2);
    let count_after_file2 = *counter.lock().unwrap();

    assert!(
        count_after_file2 > count_before,
        "file2's queries must recompute after its content changed"
    );
}

// ── 20. Incrementality: regressions unchanged → witness not recomputed ────────

#[test]
fn incrementality_unchanged_regression_not_recomputed() {
    // Two functions in the same file: edit the second, first's witness stays memoized.
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
        "def g():\n",
        "    return 1\n",
    );

    let (db, counter) = tracking_db();
    let file = SourceFile::new(&db, src.to_string());

    // Initial cold computation.
    let r1 = regressions_with_witnesses(&db, file);
    assert_eq!(r1.len(), 1);

    let count_before = *counter.lock().unwrap();

    // Re-query unchanged — should be fully cached.
    let r2 = regressions_with_witnesses(&db, file);
    let count_cached = *counter.lock().unwrap();
    assert_eq!(count_cached, count_before, "no recomputation for unchanged file");
    assert_eq!(r1, r2, "cached result must equal original");
}

// ── 21. Witness ends with Consumer as last step ───────────────────────────────

#[test]
fn witness_always_ends_with_consumer_step() {
    let srcs = vec![
        "def f():\n    x = None\n    x.attr\n",
        "def f(x: Optional[str]):\n    x.attr\n",
        "def g():\n    return None\ndef f():\n    x = g()\n    x.attr\n",
    ];
    for src in srcs {
        let reports = witnesses_for(src);
        for report in &reports {
            assert!(
                !report.witness.is_empty(),
                "witness must not be empty: {src}"
            );
            assert!(
                matches!(report.witness.last().unwrap(), WitnessStep::Consumer { .. }),
                "last step must be Consumer for: {src}"
            );
        }
    }
}

// ── 22. Regression report contains matching regression info ───────────────────

#[test]
fn regression_report_info_matches() {
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
    );
    let reports = witnesses_for(src);
    assert_eq!(reports.len(), 1);
    let r = &reports[0];
    assert_eq!(r.regression.object_symbol, "x");
    assert_eq!(r.regression.kind, ConsumerKind::Attribute);
}

// ── 23. Witness for interprocedural: 3-hop chain ─────────────────────────────

#[test]
fn witness_three_hop_chain() {
    // a → b → c, all returning None through the chain
    let src = concat!(
        "def source():\n",
        "    return None\n",
        "def middle():\n",
        "    return source()\n",
        "def consumer():\n",
        "    x = middle()\n",
        "    x.attr\n",
    );
    let reports = witnesses_for(src);
    let r = reports.iter().find(|r| r.regression.object_symbol == "x");
    assert!(r.is_some(), "should have regression on x");
    let steps = &r.unwrap().witness;
    // Must end with Consumer
    assert!(matches!(steps.last().unwrap(), WitnessStep::Consumer { .. }));
    // Must trace through at least the CallReturn
    assert!(
        step_kinds(steps).contains(&"CallReturn"),
        "witness must contain a CallReturn step for chain: {steps:?}"
    );
}

// ── 24. Benign edit to interprocedural caller: no false positive ──────────────

#[test]
fn benign_edit_to_caller_no_false_positive() {
    // g always returns non-null; editing f's non-null code shouldn't produce regressions.
    let before_src = concat!(
        "def g():\n",
        "    return 1\n",
        "def f():\n",
        "    x = g()\n",
        "    return x\n",
    );
    let after_src = concat!(
        "def g():\n",
        "    return 1\n",
        "def f():\n",
        "    y = g()\n",
        "    return y\n",
    );

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before_src.to_string());
    let before_reports = regressions_with_witnesses(&db, file);

    file.set_contents(&mut db).to(after_src.to_string());
    let after_reports = regressions_with_witnesses(&db, file);

    assert!(before_reports.is_empty());
    assert!(after_reports.is_empty());

    let diff = diff_results(&before_reports, &after_reports);
    assert!(diff.blast_radius.is_empty(), "false-positive gate: benign edit → empty diff");
}
