use salsa::Setter;
use std::sync::{Arc, Mutex};
use wake_engine::{Db, SourceFile};
use wake_prop_null::{null_regressions, null_summaries, FuncSummary, FileSummaries};
use wake_schema::{NullabilityValue, NullRegression};

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
    null_regressions(&db, file).into_iter().flat_map(|(_, r)| r).collect()
}

fn count_regressions(src: &str) -> usize {
    regressions_for(src).len()
}

fn has_regression_for(src: &str, symbol: &str) -> bool {
    regressions_for(src).iter().any(|r| r.object_symbol == symbol)
}

fn summaries_for(src: &str) -> FileSummaries {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    null_summaries(&db, file)
}

fn get_summary<'a>(sums: &'a FileSummaries, name: &str) -> Option<&'a FuncSummary> {
    sums.get(name)
}

// ── 1. Function returning None → callee always Nullable ───────────────────────

#[test]
fn returns_none_summary_base_nullable() {
    let src = "def source():\n    return None\n";
    let sums = summaries_for(src);
    let s = get_summary(&sums, "source").expect("summary for source");
    assert_eq!(s.base_return, NullabilityValue::Nullable,
        "source() always returns None → base_return = Nullable");
}

// ── 2. Caller uses return value from None-returning function ──────────────────

#[test]
fn caller_dereferences_none_returning_callee() {
    let src = concat!(
        "def source():\n",
        "    return None\n",
        "def caller():\n",
        "    x = source()\n",
        "    return x.attr\n",
    );
    assert_eq!(count_regressions(src), 1,
        "x = source() → x is Nullable → x.attr is a regression");
    assert!(has_regression_for(src, "x"));
}

// ── 3. Caller subscripts None-returning callee result ─────────────────────────

#[test]
fn caller_subscripts_none_returning_callee() {
    let src = concat!(
        "def source():\n",
        "    return None\n",
        "def caller():\n",
        "    x = source()\n",
        "    return x[0]\n",
    );
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 4. Function that propagates param to return ───────────────────────────────

#[test]
fn propagate_param_summary() {
    let src = "def propagate(x):\n    return x\n";
    let sums = summaries_for(src);
    let s = get_summary(&sums, "propagate").expect("summary for propagate");
    assert!(s.nullable_from_param[0],
        "param x being Nullable flows to return");
}

#[test]
fn caller_propagates_nullable_through_function() {
    let src = concat!(
        "def propagate(x):\n",
        "    return x\n",
        "def caller(y: Optional[str]):\n",
        "    z = propagate(y)\n",
        "    return z.upper()\n",
    );
    assert_eq!(count_regressions(src), 1,
        "y (Optional) flows through propagate → z is Nullable → z.upper() is a regression");
    assert!(has_regression_for(src, "z"));
}

// ── 5. Passing None literal to callee that dereferences it ────────────────────

#[test]
fn passing_none_literal_triggers_callee_regression() {
    let src = concat!(
        "def consumer(x):\n",
        "    return x.attr\n",
        "def caller():\n",
        "    consumer(None)\n",
    );
    // consumer(None) → x.attr fires regression inside consumer
    assert_eq!(count_regressions(src), 1,
        "None passed to consumer → x.attr fires inside consumer");
    assert!(has_regression_for(src, "x"));
}

// ── 6. Passing Nullable param to callee that dereferences it ──────────────────

#[test]
fn passing_nullable_param_triggers_callee_regression() {
    let src = concat!(
        "def consumer(x):\n",
        "    return x.attr\n",
        "def caller(y: Optional[str]):\n",
        "    consumer(y)\n",
    );
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 7. Passing NonNull to callee: no callee regression ───────────────────────

#[test]
fn passing_nonnull_to_consumer_no_regression() {
    let src = concat!(
        "def consumer(x):\n",
        "    return x.attr\n",
        "def caller(y: str):\n",
        "    consumer(y)\n",
    );
    assert_eq!(count_regressions(src), 0,
        "y is NonNull (str) → no callee regression");
}

// ── 8. Chain: a() → b() → c() — three-function None flow ─────────────────────

#[test]
fn three_function_none_chain() {
    let src = concat!(
        "def source():\n",
        "    return None\n",
        "def middle():\n",
        "    return source()\n",
        "def end():\n",
        "    x = middle()\n",
        "    return x.attr\n",
    );
    assert_eq!(count_regressions(src), 1,
        "None flows source → middle → end; x.attr is the regression");
    assert!(has_regression_for(src, "x"));
}

// ── 9. Summary: NonNull-returning function ────────────────────────────────────

#[test]
fn nonnull_returning_function_no_regression_at_caller() {
    let src = concat!(
        "def make_str():\n",
        "    return \"hello\"\n",
        "def caller():\n",
        "    x = make_str()\n",
        "    return x.upper()\n",
    );
    assert_eq!(count_regressions(src), 0,
        "make_str returns NonNull → x is NonNull → x.upper() is safe");
}

// ── 10. Unknown-returning callee: result is Unknown, no regression ────────────

#[test]
fn unknown_callee_result_is_unknown() {
    // extern_func is not defined in this file — its return is Unknown.
    let src = concat!(
        "def caller():\n",
        "    x = extern_func()\n",
        "    return x.attr\n",
    );
    assert_eq!(count_regressions(src), 0,
        "Unknown return from extern_func → x is Unknown → no regression");
}

// ── 11. Summary: param not flowing to return ──────────────────────────────────

#[test]
fn param_not_flowing_to_return_no_regression() {
    let src = concat!(
        "def safe(x):\n",
        "    return \"fixed\"\n",
        "def caller(y: Optional[str]):\n",
        "    z = safe(y)\n",
        "    return z.upper()\n",
    );
    // safe() always returns NonNull regardless of x
    assert_eq!(count_regressions(src), 0,
        "safe() always returns NonNull → z is NonNull → z.upper() is safe");
}

// ── 12. Multiple params: only the Nullable one matters ───────────────────────

#[test]
fn multi_param_nullable_flows_through() {
    let src = concat!(
        "def pick_first(a, b):\n",
        "    return a\n",
        "def caller():\n",
        "    x = pick_first(None, \"hi\")\n",
        "    return x.attr\n",
    );
    // a = None → return is Nullable → x is Nullable → regression
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

#[test]
fn multi_param_second_nullable_flows_through() {
    let src = concat!(
        "def pick_second(a, b):\n",
        "    return b\n",
        "def caller():\n",
        "    x = pick_second(\"hi\", None)\n",
        "    return x.attr\n",
    );
    // b = None → pick_second returns None → x Nullable → regression
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 13. CallStmt (discarded return): callee regression still reported ─────────

#[test]
fn callstmt_discarded_return_regression_reported() {
    let src = concat!(
        "def consumer(x):\n",
        "    x.attr\n",
        "def caller():\n",
        "    consumer(None)\n",
    );
    assert_eq!(count_regressions(src), 1,
        "consumer(None) as bare call → x.attr fires inside consumer");
}

// ── 14. No false positive: callee dereferences only non-None params ───────────

#[test]
fn callee_safe_with_nonnull_no_regression() {
    let src = concat!(
        "def process(x: str):\n",
        "    return x.upper()\n",
        "def caller():\n",
        "    result = process(\"hello\")\n",
        "    return result.lower()\n",
    );
    assert_eq!(count_regressions(src), 0);
}

// ── 15. Self-contained regression still detected (Phase 2 compatibility) ──────

#[test]
fn intraprocedural_regression_still_works() {
    let src = "def f(x: Optional[str]):\n    return x.upper()\n";
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 16. Summary base_return for propagate function ───────────────────────────

#[test]
fn propagate_summary_base_return_unknown() {
    // With all params Unknown, propagate returns Unknown (param x is Unknown)
    let src = "def propagate(x):\n    return x\n";
    let sums = summaries_for(src);
    let s = get_summary(&sums, "propagate").unwrap();
    assert_eq!(s.base_return, NullabilityValue::Unknown,
        "propagate with x=Unknown returns Unknown");
    assert_eq!(s.nullable_from_param.len(), 1);
    assert!(s.nullable_from_param[0]);
}

// ── 17. Function with no return: Unknown return in summary ───────────────────

#[test]
fn no_return_summary_base_unknown() {
    let src = "def side_effect():\n    pass\n";
    let sums = summaries_for(src);
    let s = get_summary(&sums, "side_effect").unwrap();
    assert_eq!(s.base_return, NullabilityValue::Unknown,
        "function with no return statement → Unknown base_return");
}

// ── 18. Incrementality: changing file_b doesn't recompute file_a summaries ────

#[test]
fn incrementality_summaries_independent_files() {
    let (mut db, executions) = tracking_db();

    let src_a = "def source():\n    return None\ndef caller():\n    x = source()\n    x.attr\n";
    let src_b = "def helper():\n    return \"hi\"\n";

    let file_a = SourceFile::new(&db, src_a.to_string());
    let file_b = SourceFile::new(&db, src_b.to_string());

    // Warm caches.
    let sums_a = null_summaries(&db, file_a);
    let _ = null_summaries(&db, file_b);

    // Modify file_b.
    file_b.set_contents(&mut db).to("def helper():\n    return None\n".to_string());

    // file_a summaries must not recompute.
    *executions.lock().unwrap() = 0;
    let sums_a2 = null_summaries(&db, file_a);
    assert_eq!(sums_a, sums_a2, "file_a summaries unchanged");
    assert_eq!(*executions.lock().unwrap(), 0,
        "null_summaries(file_a) must not re-execute when file_b changes");

    // file_b summaries must recompute.
    *executions.lock().unwrap() = 0;
    let _ = null_summaries(&db, file_b);
    assert!(*executions.lock().unwrap() > 0,
        "null_summaries(file_b) must re-execute after its source changes");
}

// ── 19. Incrementality: changing file_b doesn't recompute file_a regressions ──

#[test]
fn incrementality_regressions_independent_files() {
    let (mut db, executions) = tracking_db();

    let src_a = "def source():\n    return None\ndef caller():\n    x = source()\n    x.attr\n";
    let src_b = "def g(y: str):\n    y.upper()\n";

    let file_a = SourceFile::new(&db, src_a.to_string());
    let file_b = SourceFile::new(&db, src_b.to_string());

    let regs_a = null_regressions(&db, file_a);
    let _ = null_regressions(&db, file_b);

    file_b.set_contents(&mut db).to("def g(y: Optional[str]):\n    y.upper()\n".to_string());

    *executions.lock().unwrap() = 0;
    let regs_a2 = null_regressions(&db, file_a);
    assert_eq!(regs_a, regs_a2);
    assert_eq!(*executions.lock().unwrap(), 0,
        "null_regressions(file_a) must not re-execute when file_b changes");
}

// ── 20. Recursive function: no panic, returns Unknown ────────────────────────

#[test]
fn recursive_function_no_panic() {
    // fact: no callee-of-self in simple non-cyclic summary computation
    // The function calls itself → "self" not yet in summaries when computing → Unknown
    let src = concat!(
        "def fib(n):\n",
        "    if n:\n",
        "        return fib(n)\n",
        "    return None\n",
    );
    let result = std::panic::catch_unwind(|| count_regressions(src));
    assert!(result.is_ok(), "recursive function must not panic");
    // Don't assert the count — it depends on the Unknown barrier from `if`.
}

// ── 21. Multiple callee regressions: all reported ────────────────────────────

#[test]
fn multiple_callee_regressions_all_reported() {
    let src = concat!(
        "def multi_deref(x):\n",
        "    a = x.attr\n",
        "    b = x[0]\n",
        "    return b\n",
        "def caller():\n",
        "    multi_deref(None)\n",
    );
    // multi_deref with x=None → x.attr AND x[0] are both regressions
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 2, "both x.attr and x[0] regressions reported");
    assert!(regs.iter().all(|r| r.object_symbol == "x"));
}

// ── 22. Return value from call: not a consumer site on the callee ─────────────

#[test]
fn result_of_call_is_not_consumer_of_callee() {
    // `x = f()` — f is not a Nullable variable here; result is what f returns
    let src = concat!(
        "def make_none():\n",
        "    return None\n",
        "def caller():\n",
        "    x = make_none()\n",
        "    return x\n",
    );
    // No consumer regression — x is Nullable but not consumed
    let regs = regressions_for(src);
    assert_eq!(regs.len(), 0, "returning a Nullable value is not itself a regression");
}

// ── 23. Param annotation overrides callee analysis ───────────────────────────

#[test]
fn annotated_param_overrides_propagation() {
    let src = concat!(
        "def sink(x: str):\n",
        "    return x.upper()\n",
        "def caller():\n",
        "    result = sink(None)\n",
        "    return result.lower()\n",
    );
    // sink's param x is annotated str (NonNull) — annotation wins over None literal arg.
    // So x is NonNull inside sink → no regression in sink.
    // result = sink(None) → but what is sink's return? x is NonNull (annotation), returns NonNull.
    // result is NonNull → no regression at result.lower().
    assert_eq!(count_regressions(src), 0,
        "annotation NonNull wins over None argument; no regression");
}

// ── 24. Correct func_name extraction in summaries ────────────────────────────

#[test]
fn summary_keys_match_function_names() {
    let src = concat!(
        "def alpha():\n    return None\n",
        "def beta(x):\n    return x\n",
        "def gamma():\n    return \"hi\"\n",
    );
    let sums = summaries_for(src);
    assert!(get_summary(&sums, "alpha").is_some(), "alpha has a summary");
    assert!(get_summary(&sums, "beta").is_some(), "beta has a summary");
    assert!(get_summary(&sums, "gamma").is_some(), "gamma has a summary");
    assert!(get_summary(&sums, "delta").is_none(), "delta not defined");
}

// ── 25. Two-hop chain: source → relay → sink ─────────────────────────────────

#[test]
fn two_hop_chain_summary_propagation() {
    let src = concat!(
        "def source():\n",
        "    return None\n",
        "def relay():\n",
        "    return source()\n",
        "def sink():\n",
        "    x = relay()\n",
        "    x.attr\n",
    );
    // source → Nullable; relay returns source() → Nullable; sink: x = relay() → Nullable; x.attr → regression
    assert_eq!(count_regressions(src), 1);
    assert!(has_regression_for(src, "x"));
}

// ── 26. Error tolerance: parse error in callee doesn't crash caller ───────────

#[test]
fn parse_error_in_callee_no_panic() {
    // intentionally broken callee
    let src = concat!(
        "def broken(\n",  // syntax error
        "    return None\n",
        "def caller():\n",
        "    x = broken()\n",
        "    x.attr\n",
    );
    let result = std::panic::catch_unwind(|| count_regressions(src));
    assert!(result.is_ok(), "parse error in callee must not crash");
}
