//! Tests for the audit-driven fixes:
//!   #1 position-independent diff identity (no false-positive firehose)
//!   #4 real witnesses for interprocedural regressions
//!   #5 order-independent (fixpoint) summaries
//!   #9 deduplicated regressions across call sites

use salsa::Setter;
use wake_diff::{diff_results, regressions_with_witnesses, RegressionReport, WitnessStep};
use wake_engine::{Db, SourceFile};

#[salsa::db]
#[derive(Default)]
struct TestDb {
    storage: salsa::Storage<Self>,
}
#[salsa::db]
impl salsa::Database for TestDb {}
#[salsa::db]
impl Db for TestDb {}

fn reports(src: &str) -> Vec<RegressionReport> {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    regressions_with_witnesses(&db, file)
}

fn kinds(steps: &[WitnessStep]) -> Vec<&'static str> {
    steps
        .iter()
        .map(|s| match s {
            WitnessStep::NullableParam { .. } => "NullableParam",
            WitnessStep::NoneAssignment { .. } => "NoneAssignment",
            WitnessStep::VariableCopy { .. } => "VariableCopy",
            WitnessStep::CallReturn { .. } => "CallReturn",
            WitnessStep::Consumer { .. } => "Consumer",
            WitnessStep::Opaque { .. } => "Opaque",
        })
        .collect()
}

// ── #1: benign offset-shifting edits produce an empty diff ────────────────────

#[test]
fn benign_comment_insertion_is_empty_diff() {
    let before = "def f(x: Optional[str]):\n    x.attr\n";
    // Insert a leading comment line — shifts every byte offset, changes nothing.
    let after = "# a totally harmless comment\ndef f(x: Optional[str]):\n    x.attr\n";

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before.to_string());
    let b = regressions_with_witnesses(&db, file);
    file.set_contents(&mut db).to(after.to_string());
    let a = regressions_with_witnesses(&db, file);

    assert_eq!(b.len(), 1);
    assert_eq!(a.len(), 1);
    let diff = diff_results(&b, &a);
    assert!(diff.new_regressions.is_empty(), "no spurious new regressions on a comment insert");
    assert!(diff.fixed_regressions.is_empty(), "no spurious fixed regressions on a comment insert");
    assert!(diff.blast_radius.is_empty(), "empty blast radius for a semantically-irrelevant edit");
}

#[test]
fn benign_leading_blank_lines_empty_diff() {
    let before = "def g():\n    return 1\ndef f(x: Optional[str]):\n    x.attr\n";
    let after = "\n\n\ndef g():\n    return 1\ndef f(x: Optional[str]):\n    x.attr\n";
    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before.to_string());
    let b = regressions_with_witnesses(&db, file);
    file.set_contents(&mut db).to(after.to_string());
    let a = regressions_with_witnesses(&db, file);
    let diff = diff_results(&b, &a);
    assert!(diff.new_regressions.is_empty() && diff.fixed_regressions.is_empty());
    assert!(diff.blast_radius.is_empty());
}

#[test]
fn genuinely_new_regression_after_offset_shift_is_reported() {
    // A real new deref appears AND offsets shift — the new one must still surface,
    // and the pre-existing one must not be double-reported.
    let before = "def f(x: Optional[str]):\n    x.attr\n";
    let after = "# comment\ndef f(x: Optional[str]):\n    x.attr\n    x.other\n";
    let mut db = TestDb::default();
    let file = SourceFile::new(&db, before.to_string());
    let b = regressions_with_witnesses(&db, file);
    file.set_contents(&mut db).to(after.to_string());
    let a = regressions_with_witnesses(&db, file);
    let diff = diff_results(&b, &a);
    assert_eq!(diff.new_regressions.len(), 1, "only the genuinely new deref is new");
    assert!(diff.fixed_regressions.is_empty(), "the pre-existing deref is not 'fixed'");
}

// ── #4: interprocedural regressions get a real (non-Opaque) witness ───────────

#[test]
fn arg_into_callee_deref_has_real_witness() {
    let src = "def consumer(x):\n    return x.attr\ndef caller():\n    consumer(None)\n";
    let rs = reports(src);
    assert_eq!(rs.len(), 1, "one regression: x.attr inside consumer when called with None");
    let w = &rs[0].witness;
    assert!(
        !w.iter().any(|s| matches!(s, WitnessStep::Opaque { .. })),
        "witness must not be Opaque: {:?}",
        kinds(w)
    );
    assert_eq!(kinds(w), vec!["NullableParam", "Consumer"]);
    assert!(matches!(w.last().unwrap(), WitnessStep::Consumer { .. }));
}

// ── #5: summaries are order-independent (forward / transitive references) ─────

#[test]
fn transitive_forward_reference_detected() {
    // relay() is declared BEFORE the source() it depends on.
    let src = concat!(
        "def relay():\n    return source()\n",
        "def source():\n    return None\n",
        "def end():\n    x = relay()\n    x.attr\n",
    );
    let rs = reports(src);
    assert_eq!(rs.len(), 1, "None flows relay->source resolved regardless of order");
    assert_eq!(rs[0].regression.object_symbol, "x");
}

#[test]
fn order_does_not_change_result() {
    let ordered = concat!(
        "def source():\n    return None\n",
        "def relay():\n    return source()\n",
        "def end():\n    x = relay()\n    x.attr\n",
    );
    let reversed = concat!(
        "def relay():\n    return source()\n",
        "def source():\n    return None\n",
        "def end():\n    x = relay()\n    x.attr\n",
    );
    assert_eq!(reports(ordered).len(), reports(reversed).len(), "declaration order is irrelevant");
}

// ── #9: a callee deref reached from several sites is reported once ────────────

#[test]
fn duplicate_call_sites_dedup() {
    let src = concat!(
        "def consumer(x):\n    return x.attr\n",
        "def a():\n    consumer(None)\n",
        "def b():\n    consumer(None)\n",
        "def c():\n    consumer(None)\n",
    );
    let rs = reports(src);
    assert_eq!(rs.len(), 1, "x.attr in consumer is one regression despite three callers");
}
