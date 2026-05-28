//! Phase 2 control-flow analysis: branch/loop narrowing.
//! The cardinal rule is precision over soundness — guarded derefs must NOT be
//! reported (no false positives), while genuinely unguarded derefs inside
//! control flow must be caught.

use wake_engine::{Db, SourceFile};
use wake_prop_null::null_regressions;
use wake_schema::NullRegression;

#[salsa::db]
#[derive(Default)]
struct TestDb {
    storage: salsa::Storage<Self>,
}
#[salsa::db]
impl salsa::Database for TestDb {}
#[salsa::db]
impl Db for TestDb {}

fn regs(src: &str) -> Vec<NullRegression> {
    let db = TestDb::default();
    let file = SourceFile::new(&db, src.to_string());
    null_regressions(&db, file).into_iter().flat_map(|(_, r)| r).collect()
}
fn count(src: &str) -> usize {
    regs(src).len()
}

// ── Precision: guarded derefs must not be reported (no false positives) ───────

#[test]
fn guard_is_not_none_no_fp() {
    let src = "def f(x: Optional[str]):\n    if x is not None:\n        x.upper()\n";
    assert_eq!(count(src), 0, "x narrowed to NonNull inside `if x is not None`");
}

#[test]
fn guard_truthy_no_fp() {
    let src = "def f(x: Optional[str]):\n    if x:\n        x.upper()\n";
    assert_eq!(count(src), 0, "x narrowed to NonNull inside `if x`");
}

#[test]
fn guard_equality_none_else_no_fp() {
    let src = "def f(x: Optional[str]):\n    if x == None:\n        pass\n    else:\n        x.upper()\n";
    assert_eq!(count(src), 0, "else of `x == None` narrows x to NonNull");
}

#[test]
fn guard_not_x_else_no_fp() {
    let src = "def f(x: Optional[str]):\n    if not x:\n        return\n    x.upper()\n";
    // `if not x: return` — after the branch the env joins to Unknown (precision-safe).
    assert_eq!(count(src), 0, "no false positive after a `not x` early return");
}

#[test]
fn unrecognized_guard_on_var_no_fp() {
    // Condition references x in a way we can't interpret → x set Unknown in the
    // arm, so no false positive even though we don't understand the guard.
    let src = "def f(x: Optional[str]):\n    if validate(x):\n        x.upper()\n";
    assert_eq!(count(src), 0, "opaque guard referencing x suppresses the report");
}

#[test]
fn for_over_optional_no_fp() {
    let src = "def f(x: Optional[list]):\n    for i in x:\n        pass\n    return x[0]\n";
    assert_eq!(count(src), 0, "iterating x proves it non-None; later x[0] is not a FP");
}

#[test]
fn while_guarded_no_fp() {
    let src = "def f(x: Optional[object]):\n    while x is not None:\n        x.method()\n";
    assert_eq!(count(src), 0, "x narrowed NonNull inside `while x is not None`");
}

// ── Recall: genuinely unguarded derefs inside control flow must be caught ─────

#[test]
fn deref_inside_unrelated_branch_is_caught() {
    let src = "def f(x: Optional[str], flag):\n    if flag:\n        x.upper()\n";
    assert_eq!(count(src), 1, "x is unguarded inside the branch → regression");
    assert_eq!(regs(src)[0].object_symbol, "x");
}

#[test]
fn deref_after_confirming_none_is_caught() {
    let src = "def f(x: Optional[str]):\n    if x is None:\n        x.upper()\n";
    assert_eq!(count(src), 1, "deref of x in the branch where it IS None → regression");
}

#[test]
fn deref_in_else_when_none_on_that_path_is_caught() {
    let src = "def f(x: Optional[str]):\n    if x is not None:\n        pass\n    else:\n        x.upper()\n";
    assert_eq!(count(src), 1, "else of `is not None` → x is None there → regression");
}

#[test]
fn deref_in_loop_body_is_caught() {
    let src = "def f(x: Optional[str], items):\n    for i in items:\n        x.upper()\n";
    assert_eq!(count(src), 1, "x unguarded inside an unrelated for-loop → regression");
}

#[test]
fn none_assign_inside_branch_then_deref_after() {
    // x assigned None in one branch, NonNull-ish in the other → join is Unknown,
    // so no report after (precision-safe). But deref *inside* the None branch fires.
    let src = "def f(flag):\n    if flag:\n        x = None\n        x.attr\n    else:\n        x = 1\n";
    assert_eq!(count(src), 1, "x.attr inside the branch where x = None → regression");
}

#[test]
fn nested_branches_narrowing_composes() {
    let src = "def f(x: Optional[str], y):\n    if y:\n        if x is not None:\n            x.upper()\n";
    assert_eq!(count(src), 0, "nested narrowing keeps x NonNull");
}

// ── elif chains ───────────────────────────────────────────────────────────────

#[test]
fn elif_branch_deref_caught() {
    let src = "def f(x: Optional[str], a, b):\n    if a:\n        pass\n    elif b:\n        x.upper()\n";
    assert_eq!(count(src), 1, "deref in an elif body is visible and unguarded");
}

#[test]
fn elif_guard_no_fp() {
    let src = "def f(x: Optional[str], a):\n    if a:\n        pass\n    elif x is not None:\n        x.upper()\n";
    assert_eq!(count(src), 0, "elif condition narrows x in its own body");
}

// ── assert as an unconditional guard ──────────────────────────────────────────

#[test]
fn assert_is_not_none_guards_following_deref() {
    let src = "def f(x: Optional[str]):\n    assert x is not None\n    return x.upper()\n";
    assert_eq!(count(src), 0, "assert x is not None narrows x for the rest of the function");
}

#[test]
fn assert_truthy_guards_following_deref() {
    let src = "def f(x: Optional[str]):\n    assert x\n    return x.upper()\n";
    assert_eq!(count(src), 0, "assert x narrows x to NonNull");
}

#[test]
fn assert_unrelated_does_not_hide_real_bug() {
    let src = "def f(x: Optional[str], y):\n    assert y\n    return x.upper()\n";
    assert_eq!(count(src), 1, "assert on an unrelated var must not suppress x's regression");
}

// ── try/with/match remain opaque barriers (precision-safe, documented) ────────

#[test]
fn try_block_is_opaque_no_report() {
    let src = "def f(x: Optional[str]):\n    try:\n        x.upper()\n    except Exception:\n        pass\n";
    assert_eq!(count(src), 0, "try is a not-yet-modeled opaque barrier (no FP, no report)");
}
