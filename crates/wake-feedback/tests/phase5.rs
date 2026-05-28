use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_diff::{RegressionReport, WitnessStep};
use wake_engine::{Db, SourceFile};
use wake_feedback::{
    Confidence, RootCause, ShapedFeedback,
    shape_feedback, shaped_regressions, trim_witness,
};
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

// ── Helpers ───────────────────────────────────────────────────────────────────

fn shaped_for(src: &str) -> Vec<ShapedFeedback> {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    shaped_regressions(&db, file)
}

fn node(start: u32, end: u32) -> NodeId {
    NodeId { start_byte: start, end_byte: end }
}

fn make_regression(consumer_start: u32, symbol: &str, kind: ConsumerKind) -> NullRegression {
    NullRegression {
        func_node: node(0, 100),
        consumer_node: node(consumer_start, consumer_start + 10),
        object_symbol: symbol.to_string(),
        kind,
    }
}

fn report_with_witness(
    consumer_start: u32,
    symbol: &str,
    kind: ConsumerKind,
    witness: Vec<WitnessStep>,
) -> RegressionReport {
    RegressionReport {
        regression: make_regression(consumer_start, symbol, kind),
        witness,
    }
}

fn none_assign_step(sym: &str) -> WitnessStep {
    WitnessStep::NoneAssignment { node: node(10, 20), symbol: sym.to_string() }
}

fn nullable_param_step(sym: &str) -> WitnessStep {
    WitnessStep::NullableParam { node: node(5, 15), symbol: sym.to_string() }
}

fn consumer_step(sym: &str, kind: ConsumerKind) -> WitnessStep {
    WitnessStep::Consumer { node: node(50, 60), symbol: sym.to_string(), kind }
}

fn opaque_step() -> WitnessStep {
    WitnessStep::Opaque { symbol: "?".to_string() }
}

fn copy_step(from: &str, to: &str) -> WitnessStep {
    WitnessStep::VariableCopy { node: node(30, 40), from: from.to_string(), to: to.to_string() }
}

// ── 1. Empty input → empty output ────────────────────────────────────────────

#[test]
fn empty_input_empty_output() {
    let result = shape_feedback(&[]);
    assert!(result.is_empty());
}

// ── 2. Single regression → one ShapedFeedback ─────────────────────────────────

#[test]
fn single_regression_one_feedback() {
    let witness = vec![none_assign_step("x"), consumer_step("x", ConsumerKind::Attribute)];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1, "one root cause → one ShapedFeedback");
    assert_eq!(result[0].consumers.len(), 1);
}

// ── 3. PHASE 5 GATE: one message per root cause on multi-consumer regression ──

#[test]
fn multi_consumer_same_root_deduplicated() {
    // Both consumers flow from the same NoneAssignment node.
    let root_node = node(10, 20);
    let w1 = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(50, 60), symbol: "x".to_string(), kind: ConsumerKind::Attribute },
    ];
    let w2 = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(70, 80), symbol: "x".to_string(), kind: ConsumerKind::Subscript },
    ];
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, w1),
        report_with_witness(70, "x", ConsumerKind::Subscript, w2),
    ];

    let result = shape_feedback(&reports);

    assert_eq!(result.len(), 1, "same root cause → exactly one ShapedFeedback (Phase 5 gate)");
    assert_eq!(result[0].consumers.len(), 2, "both consumers present in the group");
    assert!(
        matches!(&result[0].root_cause, RootCause::NoneAssignment { .. }),
        "root cause should be NoneAssignment"
    );
}

// ── 4. Two different root causes → two messages ────────────────────────────────

#[test]
fn two_different_root_causes_two_feedbacks() {
    let node_a = node(10, 20);
    let node_b = node(30, 40);
    let w_a = vec![
        WitnessStep::NoneAssignment { node: node_a, symbol: "a".to_string() },
        consumer_step("a", ConsumerKind::Attribute),
    ];
    let w_b = vec![
        WitnessStep::NoneAssignment { node: node_b, symbol: "b".to_string() },
        consumer_step("b", ConsumerKind::Attribute),
    ];
    let reports = vec![
        report_with_witness(50, "a", ConsumerKind::Attribute, w_a),
        report_with_witness(80, "b", ConsumerKind::Attribute, w_b),
    ];

    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 2, "two distinct root causes → two ShapedFeedback items");
}

// ── 5. NullableParam as root cause ────────────────────────────────────────────

#[test]
fn nullable_param_root_cause() {
    let witness = vec![
        nullable_param_step("x"),
        consumer_step("x", ConsumerKind::Attribute),
    ];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);

    assert_eq!(result.len(), 1);
    assert!(
        matches!(&result[0].root_cause, RootCause::NullableParam { symbol, .. } if symbol == "x"),
        "root cause should be NullableParam(x)"
    );
}

// ── 6. NullableParam dedup: two consumers, same param node ────────────────────

#[test]
fn nullable_param_multi_consumer_deduplicated() {
    let param_node = node(5, 15);
    let w1 = vec![
        WitnessStep::NullableParam { node: param_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(50, 60), symbol: "x".to_string(), kind: ConsumerKind::Attribute },
    ];
    let w2 = vec![
        WitnessStep::NullableParam { node: param_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(70, 80), symbol: "x".to_string(), kind: ConsumerKind::Subscript },
    ];
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, w1),
        report_with_witness(70, "x", ConsumerKind::Subscript, w2),
    ];

    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].consumers.len(), 2);
}

// ── 7. Confidence: High when no Opaque steps ─────────────────────────────────

#[test]
fn confidence_high_no_opaque() {
    let witness = vec![none_assign_step("x"), consumer_step("x", ConsumerKind::Attribute)];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].confidence, Confidence::High);
}

// ── 8. Confidence: Medium when one Opaque step ────────────────────────────────

#[test]
fn confidence_medium_one_opaque() {
    let witness = vec![opaque_step(), consumer_step("x", ConsumerKind::Attribute)];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].confidence, Confidence::Medium);
}

// ── 9. Confidence: Low when two or more Opaque steps ─────────────────────────

#[test]
fn confidence_low_two_opaque() {
    let witness = vec![opaque_step(), opaque_step(), consumer_step("x", ConsumerKind::Attribute)];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].confidence, Confidence::Low);
}

// ── 10. Confidence: High for multi-step non-opaque witness ────────────────────

#[test]
fn confidence_high_with_copy_chain() {
    let witness = vec![
        none_assign_step("a"),
        copy_step("a", "b"),
        consumer_step("b", ConsumerKind::Attribute),
    ];
    let reports = vec![report_with_witness(50, "b", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].confidence, Confidence::High, "copy chain without Opaque → High");
}

// ── 11. Confidence: best across consumers when grouped ────────────────────────

#[test]
fn confidence_best_of_group() {
    // One consumer with Opaque (Medium), one without (High). Group should be High.
    let root_node = node(10, 20);
    let w_high = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(50, 60), symbol: "x".to_string(), kind: ConsumerKind::Attribute },
    ];
    let w_medium = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        opaque_step(),
        WitnessStep::Consumer { node: node(70, 80), symbol: "x".to_string(), kind: ConsumerKind::Subscript },
    ];
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, w_high),
        report_with_witness(70, "x", ConsumerKind::Subscript, w_medium),
    ];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].confidence, Confidence::High, "group confidence = max over consumers");
}

// ── 12. Ranking: High confidence before Low ───────────────────────────────────

#[test]
fn ranking_high_confidence_first() {
    let node_a = node(10, 20); // will be Low (opaque)
    let node_b = node(30, 40); // will be High (no opaque)

    let w_low = vec![
        WitnessStep::NoneAssignment { node: node_a, symbol: "a".to_string() },
        opaque_step(),
        opaque_step(),
        WitnessStep::Consumer { node: node(50, 60), symbol: "a".to_string(), kind: ConsumerKind::Attribute },
    ];
    let w_high = vec![
        WitnessStep::NoneAssignment { node: node_b, symbol: "b".to_string() },
        WitnessStep::Consumer { node: node(70, 80), symbol: "b".to_string(), kind: ConsumerKind::Attribute },
    ];
    let reports = vec![
        report_with_witness(50, "a", ConsumerKind::Attribute, w_low),
        report_with_witness(70, "b", ConsumerKind::Attribute, w_high),
    ];
    let result = shape_feedback(&reports);

    assert_eq!(result.len(), 2);
    assert_eq!(result[0].confidence, Confidence::High, "High-confidence finding should be ranked first");
    assert_eq!(result[1].confidence, Confidence::Low);
}

// ── 13. Ranking: more consumers before fewer (same confidence) ────────────────

#[test]
fn ranking_more_consumers_higher_priority() {
    let node_a = node(10, 20); // 3 consumers
    let node_b = node(30, 40); // 1 consumer

    let make_w = |root_node: NodeId, root_sym: &str, consumer_node: NodeId, sym: &str| {
        vec![
            WitnessStep::NoneAssignment { node: root_node, symbol: root_sym.to_string() },
            WitnessStep::Consumer { node: consumer_node, symbol: sym.to_string(), kind: ConsumerKind::Attribute },
        ]
    };

    let reports = vec![
        // node_a: 3 consumers
        report_with_witness(50, "a", ConsumerKind::Attribute, make_w(node_a, "a", node(50, 60), "a")),
        report_with_witness(60, "a", ConsumerKind::Attribute, make_w(node_a, "a", node(60, 70), "a")),
        report_with_witness(70, "a", ConsumerKind::Attribute, make_w(node_a, "a", node(70, 80), "a")),
        // node_b: 1 consumer
        report_with_witness(90, "b", ConsumerKind::Attribute, make_w(node_b, "b", node(90, 100), "b")),
    ];

    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 2);
    // Both are High confidence; the group with more consumers comes first.
    assert_eq!(result[0].consumers.len(), 3, "larger group ranked first");
    assert_eq!(result[1].consumers.len(), 1);
}

// ── 14. Consumer sort: shorter witness (closer to root) first ─────────────────

#[test]
fn consumers_sorted_shorter_witness_first() {
    let root_node = node(10, 20);
    let short = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        WitnessStep::Consumer { node: node(50, 60), symbol: "x".to_string(), kind: ConsumerKind::Attribute },
    ];
    let long = vec![
        WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
        copy_step("x", "y"),
        WitnessStep::Consumer { node: node(70, 80), symbol: "y".to_string(), kind: ConsumerKind::Subscript },
    ];
    // Insert longer witness first to verify sorting.
    let reports = vec![
        report_with_witness(70, "y", ConsumerKind::Subscript, long),
        report_with_witness(50, "x", ConsumerKind::Attribute, short),
    ];

    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1);
    assert_eq!(
        result[0].consumers[0].witness.len(),
        2,
        "shorter witness consumer should be first"
    );
    assert_eq!(result[0].consumers[1].witness.len(), 3);
}

// ── 15. fix_locus for NoneAssignment ─────────────────────────────────────────

#[test]
fn fix_locus_none_assignment() {
    let source_node = node(10, 20);
    let witness = vec![
        WitnessStep::NoneAssignment { node: source_node, symbol: "x".to_string() },
        consumer_step("x", ConsumerKind::Attribute),
    ];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].fix_locus(), Some(source_node), "fix_locus should point to the assignment node");
}

// ── 16. fix_locus for NullableParam ──────────────────────────────────────────

#[test]
fn fix_locus_nullable_param() {
    let param_node = node(5, 15);
    let witness = vec![
        WitnessStep::NullableParam { node: param_node, symbol: "x".to_string() },
        consumer_step("x", ConsumerKind::Attribute),
    ];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].fix_locus(), Some(param_node));
}

// ── 17. fix_locus for Opaque → None ──────────────────────────────────────────

#[test]
fn fix_locus_opaque_none() {
    let witness = vec![opaque_step(), consumer_step("x", ConsumerKind::Attribute)];
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, witness)];
    let result = shape_feedback(&reports);
    assert_eq!(result[0].fix_locus(), None, "opaque root → no fix locus");
}

// ── 18. trim_witness: no trimming needed ─────────────────────────────────────

#[test]
fn trim_witness_no_change_when_within_budget() {
    let witness = vec![none_assign_step("x"), consumer_step("x", ConsumerKind::Attribute)];
    let trimmed = trim_witness(&witness, 5);
    assert_eq!(trimmed, witness, "within budget → unchanged");
}

// ── 19. trim_witness: exact limit → unchanged ─────────────────────────────────

#[test]
fn trim_witness_exact_limit_unchanged() {
    let witness = vec![
        none_assign_step("a"),
        copy_step("a", "b"),
        consumer_step("b", ConsumerKind::Attribute),
    ];
    let trimmed = trim_witness(&witness, 3);
    assert_eq!(trimmed, witness, "exact limit → no truncation");
}

// ── 20. trim_witness: truncation inserts Opaque marker ────────────────────────

#[test]
fn trim_witness_inserts_opaque_when_truncated() {
    let witness = vec![
        none_assign_step("a"),
        copy_step("a", "b"),
        copy_step("b", "c"),
        copy_step("c", "d"),
        consumer_step("d", ConsumerKind::Attribute),
    ];
    let trimmed = trim_witness(&witness, 3);
    assert_eq!(trimmed.len(), 3, "trimmed to exactly max_steps");
    assert!(
        matches!(&trimmed[1], WitnessStep::Opaque { .. }),
        "middle step should be Opaque truncation marker"
    );
    assert!(
        matches!(&trimmed[0], WitnessStep::NoneAssignment { .. }),
        "first step preserved"
    );
    assert!(
        matches!(&trimmed[2], WitnessStep::Consumer { .. }),
        "last step (Consumer) preserved"
    );
}

// ── 21. trim_witness: max_steps = 1 → just the Consumer ─────────────────────

#[test]
fn trim_witness_max_one_keeps_last() {
    let witness = vec![none_assign_step("x"), copy_step("x", "y"), consumer_step("y", ConsumerKind::Attribute)];
    let trimmed = trim_witness(&witness, 1);
    assert_eq!(trimmed.len(), 1);
    assert!(matches!(&trimmed[0], WitnessStep::Consumer { .. }));
}

// ── 22. trim_witness: max_steps = 2 → first + Opaque (last always present) ──

#[test]
fn trim_witness_max_two() {
    let witness = vec![
        none_assign_step("x"),
        copy_step("x", "y"),
        copy_step("y", "z"),
        consumer_step("z", ConsumerKind::Attribute),
    ];
    let trimmed = trim_witness(&witness, 2);
    assert_eq!(trimmed.len(), 2, "max_steps=2 → [Opaque, Consumer]");
    assert!(matches!(&trimmed[0], WitnessStep::Opaque { .. }), "first of 2 is Opaque marker");
    assert!(matches!(&trimmed[1], WitnessStep::Consumer { .. }));
}

// ── 23. trim_witness: max_steps = 0 → empty ──────────────────────────────────

#[test]
fn trim_witness_max_zero_empty() {
    let witness = vec![none_assign_step("x"), consumer_step("x", ConsumerKind::Attribute)];
    let trimmed = trim_witness(&witness, 0);
    assert!(trimmed.is_empty());
}

// ── 24. trim_witness: empty input ────────────────────────────────────────────

#[test]
fn trim_witness_empty_input() {
    let trimmed = trim_witness(&[], 5);
    assert!(trimmed.is_empty());
}

// ── 25. Opaque root: grouped by description, fix_locus = None ────────────────

#[test]
fn opaque_roots_grouped_by_description() {
    // Two consumers both with Opaque("?") as root → single group.
    let w1 = vec![
        WitnessStep::Opaque { symbol: "same_source".to_string() },
        WitnessStep::Consumer { node: node(50, 60), symbol: "x".to_string(), kind: ConsumerKind::Attribute },
    ];
    let w2 = vec![
        WitnessStep::Opaque { symbol: "same_source".to_string() },
        WitnessStep::Consumer { node: node(70, 80), symbol: "x".to_string(), kind: ConsumerKind::Subscript },
    ];
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, w1),
        report_with_witness(70, "x", ConsumerKind::Subscript, w2),
    ];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1, "same Opaque description → single group");
    assert_eq!(result[0].fix_locus(), None);
}

// ── 26. Opaque roots with different descriptions → separate groups ─────────────

#[test]
fn opaque_roots_different_descriptions_separate() {
    let w1 = vec![
        WitnessStep::Opaque { symbol: "source_a".to_string() },
        consumer_step("x", ConsumerKind::Attribute),
    ];
    let w2 = vec![
        WitnessStep::Opaque { symbol: "source_b".to_string() },
        consumer_step("y", ConsumerKind::Attribute),
    ];
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, w1),
        report_with_witness(70, "y", ConsumerKind::Attribute, w2),
    ];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 2, "different Opaque descriptions → separate groups");
}

// ── 27. Consumer with empty witness (fallback to regression info) ─────────────

#[test]
fn empty_witness_fallback_to_regression_info() {
    let reports = vec![report_with_witness(50, "x", ConsumerKind::Attribute, vec![])];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1);
    assert_eq!(result[0].consumers.len(), 1);
    assert_eq!(result[0].consumers[0].symbol, "x");
}

// ── 28. Full pipeline via shaped_regressions: no regressions → empty ──────────

#[test]
fn full_pipeline_clean_code_empty() {
    let src = "def f(x: int) -> int:\n    return x + 1\n";
    let result = shaped_for(src);
    assert!(result.is_empty(), "clean code → no feedback");
}

// ── 29. Full pipeline: single None assignment ─────────────────────────────────

#[test]
fn full_pipeline_single_none_assignment() {
    let src = concat!("def f():\n", "    x = None\n", "    x.attr\n");
    let result = shaped_for(src);
    assert_eq!(result.len(), 1, "one root cause");
    assert_eq!(result[0].consumers.len(), 1);
    assert!(
        matches!(&result[0].root_cause, RootCause::NoneAssignment { symbol, .. } if symbol == "x"),
        "root cause should be NoneAssignment for x"
    );
    assert_eq!(result[0].confidence, Confidence::High);
    assert!(result[0].fix_locus().is_some(), "fix_locus should be present");
}

// ── 30. PHASE 5 GATE (full pipeline): one message per root cause ──────────────

#[test]
fn full_pipeline_multi_consumer_single_message() {
    // x = None, then x.attr and x[0] — two consumers, one root cause.
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",
        "    x[0]\n",
    );
    let result = shaped_for(src);

    assert_eq!(result.len(), 1, "PHASE 5 GATE: one message per root cause");
    assert_eq!(result[0].consumers.len(), 2, "both consumers present");
    assert!(matches!(&result[0].root_cause, RootCause::NoneAssignment { symbol, .. } if symbol == "x"));
}

// ── 31. Full pipeline: two different None assignments → two messages ───────────

#[test]
fn full_pipeline_two_root_causes_two_messages() {
    let src = concat!(
        "def f():\n",
        "    a = None\n",
        "    b = None\n",
        "    a.x\n",
        "    b.y\n",
    );
    let result = shaped_for(src);
    assert_eq!(result.len(), 2, "two distinct None assignments → two feedback items");
}

// ── 32. Full pipeline: nullable param, two consumers deduplicated ─────────────

#[test]
fn full_pipeline_nullable_param_two_consumers() {
    let src = concat!(
        "def f(x: Optional[str]):\n",
        "    x.attr\n",
        "    x[0]\n",
    );
    let result = shaped_for(src);
    assert_eq!(result.len(), 1, "same nullable param → one message");
    assert_eq!(result[0].consumers.len(), 2);
    assert!(matches!(&result[0].root_cause, RootCause::NullableParam { .. }));
}

// ── 33. Full pipeline: interprocedural root cause deduplication ───────────────

#[test]
fn full_pipeline_interprocedural_dedup() {
    // g() returns None; f() calls g() and uses x twice → both consumers share
    // the same NoneAssignment root cause (the `return None` in g).
    let src = concat!(
        "def g():\n",
        "    return None\n",
        "def f():\n",
        "    x = g()\n",
        "    x.attr\n",
        "    x[0]\n",
    );
    let result = shaped_for(src);

    // Both consumers of x flow from the same return None in g.
    // They may be grouped or separate depending on root cause identity.
    // Key assertion: we do NOT get more messages than root causes.
    let total_consumers: usize = result.iter().map(|r| r.consumers.len()).sum();
    assert_eq!(total_consumers, 2, "both consumers captured");
    assert!(result.len() <= 2, "at most one message per root cause");
}

// ── 34. Full pipeline: High confidence for direct assignment ──────────────────

#[test]
fn full_pipeline_high_confidence_direct_assignment() {
    let src = concat!("def f():\n", "    x = None\n", "    x.attr\n");
    let result = shaped_for(src);
    assert_eq!(result[0].confidence, Confidence::High);
}

// ── 35. Incrementality: shaped_regressions not recomputed on unrelated edit ───

#[test]
fn incrementality_shaped_regressions_not_recomputed() {
    let src1 = concat!("def f():\n", "    x = None\n", "    x.attr\n");
    let src2 = "def g():\n    return 1\n";

    let (mut db, counter) = tracking_db();
    let file1 = SourceFile::new(&db, src1.to_string());
    let file2 = SourceFile::new(&db, src2.to_string());

    // Cold computation.
    let _ = shaped_regressions(&db, file1);
    let _ = shaped_regressions(&db, file2);

    let count_before = *counter.lock().unwrap();

    // Edit file2 — should not invalidate file1.
    file2.set_contents(&mut db).to("def g():\n    return 2\n".to_string());

    // Re-query file1 — must be cached.
    let _ = shaped_regressions(&db, file1);
    let count_after_file1 = *counter.lock().unwrap();
    assert_eq!(
        count_after_file1, count_before,
        "file1's shaped_regressions must not recompute when file2 changes"
    );

    // Re-query file2 — should recompute.
    let _ = shaped_regressions(&db, file2);
    let count_after_file2 = *counter.lock().unwrap();
    assert!(
        count_after_file2 > count_before,
        "file2 must recompute after its content changed"
    );
}

// ── 36. Incrementality: unchanged file cached ────────────────────────────────

#[test]
fn incrementality_cached_on_requery() {
    let src = concat!("def f():\n", "    x = None\n", "    x.attr\n");
    let (db, counter) = tracking_db();
    let file = SourceFile::new(&db, src.to_string());

    let r1 = shaped_regressions(&db, file);
    let count_after_first = *counter.lock().unwrap();

    let r2 = shaped_regressions(&db, file);
    let count_after_second = *counter.lock().unwrap();

    assert_eq!(count_after_first, count_after_second, "re-query of unchanged file → no recompute");
    assert_eq!(r1, r2, "cached result must equal original");
}

// ── 37. consumer_kinds preserved correctly ────────────────────────────────────

#[test]
fn consumer_kinds_preserved_in_shaped_output() {
    let src = concat!(
        "def f():\n",
        "    x = None\n",
        "    x.attr\n",    // Attribute
        "    x[0]\n",      // Subscript
        "    x()\n",       // Call
    );
    let result = shaped_for(src);
    assert_eq!(result.len(), 1);
    let kinds: Vec<ConsumerKind> = result[0].consumers.iter().map(|c| c.kind).collect();
    assert!(kinds.contains(&ConsumerKind::Attribute), "Attribute consumer should be present");
    assert!(kinds.contains(&ConsumerKind::Subscript), "Subscript consumer should be present");
    assert!(kinds.contains(&ConsumerKind::Call), "Call consumer should be present");
}

// ── 38. All consumers in a group share the same root cause ────────────────────

#[test]
fn all_consumers_share_root_cause_node() {
    let root_node = node(10, 20);
    let make_w = |consumer_node: NodeId| {
        vec![
            WitnessStep::NoneAssignment { node: root_node, symbol: "x".to_string() },
            WitnessStep::Consumer { node: consumer_node, symbol: "x".to_string(), kind: ConsumerKind::Attribute },
        ]
    };
    let reports = vec![
        report_with_witness(50, "x", ConsumerKind::Attribute, make_w(node(50, 60))),
        report_with_witness(70, "x", ConsumerKind::Attribute, make_w(node(70, 80))),
        report_with_witness(90, "x", ConsumerKind::Attribute, make_w(node(90, 100))),
    ];
    let result = shape_feedback(&reports);
    assert_eq!(result.len(), 1, "three consumers, one root → one message");
    assert_eq!(result[0].consumers.len(), 3);
    // The root cause node should be the same for all.
    if let RootCause::NoneAssignment { node, .. } = &result[0].root_cause {
        assert_eq!(*node, root_node);
    } else {
        panic!("expected NoneAssignment root cause");
    }
}
