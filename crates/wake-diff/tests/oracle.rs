//! #3 — the mandated from-scratch differential oracle (design §6.8).
//!
//! On a randomized stream of edits, the *incremental* analysis (one database,
//! `set_contents` per edit) must equal the *from-scratch* analysis (a fresh
//! database built from the same final text). A mismatch means salsa
//! invalidation produced a stale fact — the highest-risk correctness bug class.
//!
//! We also include a property-style check of the lattice join and a
//! benign-edit stability sweep.

use salsa::Setter;
use wake_diff::{regressions_with_witnesses, workspace_regressions_with_witnesses};
use wake_engine::{Db, SourceFile, Workspace};
use wake_prop_null::{null_regressions, null_summaries, workspace_regressions, workspace_summaries};
use wake_schema::{NullRegression, NullabilityValue};

#[salsa::db]
#[derive(Default)]
struct TestDb {
    storage: salsa::Storage<Self>,
}
#[salsa::db]
impl salsa::Database for TestDb {}
#[salsa::db]
impl Db for TestDb {}

// ── Deterministic PRNG (xorshift64) — no external dependency ───────────────────

struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

/// A pool of self-contained top-level fragments — straight-line, control-flow,
/// interprocedural, clean, buggy, and offset-shifting (comment) variants.
fn fragments() -> Vec<&'static str> {
    vec![
        "def src():\n    return None\n",
        "def clean():\n    return 1\n",
        "def use_src():\n    x = src()\n    return x.attr\n",
        "def guarded(x: Optional[str]):\n    if x is not None:\n        return x.upper()\n    return None\n",
        "def buggy(x: Optional[str]):\n    return x.upper()\n",
        "def loopy(x: Optional[list]):\n    for i in x:\n        x.append(i)\n",
        "def passthrough(y):\n    return y\n",
        "def caller():\n    z = passthrough(None)\n    z.attr\n",
        "# a comment that only shifts byte offsets\n",
        "def relay():\n    return src()\n",
        "def branchy(a, b):\n    if a:\n        c = None\n    else:\n        c = 1\n    return b\n",
        "def deep(x: Optional[str]):\n    y = x\n    z = y\n    return z.attr\n",
    ]
}

fn assemble(indices: &[usize], frags: &[&str]) -> String {
    indices.iter().map(|&i| frags[i]).collect()
}

// ── Canonicalization (analysis output is deterministic but we sort to be safe) ─

fn reg_key(r: &NullRegression) -> (String, u32, u32, String, u8) {
    (r.func_name.clone(), r.consumer_node.start_byte, r.consumer_node.end_byte, r.object_symbol.clone(), r.kind as u8)
}

fn canon_regressions(db: &TestDb, file: SourceFile) -> Vec<NullRegression> {
    let mut all: Vec<NullRegression> =
        null_regressions(db, file).into_iter().flat_map(|(_, r)| r).collect();
    all.sort_by_key(reg_key);
    all
}

fn canon_reports(db: &TestDb, file: SourceFile) -> Vec<(NullRegression, usize)> {
    let mut reports: Vec<(NullRegression, usize)> = regressions_with_witnesses(db, file)
        .into_iter()
        .map(|r| (r.regression, r.witness.len()))
        .collect();
    reports.sort_by_key(|(r, _)| reg_key(r));
    reports
}

#[test]
fn incremental_matches_scratch_on_random_edits() {
    let frags = fragments();
    for seed in 1..=8u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E3779B97F4A7C15));

        // Incremental database: one file handle, edited in place.
        let mut inc_db = TestDb::default();
        let mut program: Vec<usize> = (0..3).map(|_| rng.below(frags.len())).collect();
        let inc_file = SourceFile::new(&inc_db, assemble(&program, &frags));

        for step in 0..70 {
            // Mutate the program.
            match rng.below(3) {
                0 => program.push(rng.below(frags.len())),
                1 if !program.is_empty() => {
                    let i = rng.below(program.len());
                    program.remove(i);
                }
                _ => {
                    if program.is_empty() {
                        program.push(rng.below(frags.len()));
                    } else {
                        let i = rng.below(program.len());
                        program[i] = rng.below(frags.len());
                    }
                }
            }
            let text = assemble(&program, &frags);

            // Apply incrementally.
            inc_file.set_contents(&mut inc_db).to(text.clone());

            // Compute from scratch on a brand-new database.
            let scratch_db = TestDb::default();
            let scratch_file = SourceFile::new(&scratch_db, text.clone());

            assert_eq!(
                null_summaries(&inc_db, inc_file),
                null_summaries(&scratch_db, scratch_file),
                "summaries diverged at seed {seed} step {step}\n--- text ---\n{text}"
            );
            assert_eq!(
                canon_regressions(&inc_db, inc_file),
                canon_regressions(&scratch_db, scratch_file),
                "regressions diverged at seed {seed} step {step}\n--- text ---\n{text}"
            );
            assert_eq!(
                canon_reports(&inc_db, inc_file),
                canon_reports(&scratch_db, scratch_file),
                "regression reports/witnesses diverged at seed {seed} step {step}\n--- text ---\n{text}"
            );
        }
    }
}

// ── Cross-file oracle: workspace incremental == workspace from-scratch ─────────

fn ws_reg_key(r: &NullRegression) -> (String, String, u32, u32, String, u8) {
    (
        r.file.clone(),
        r.func_name.clone(),
        r.consumer_node.start_byte,
        r.consumer_node.end_byte,
        r.object_symbol.clone(),
        r.kind as u8,
    )
}

fn canon_ws_regressions(db: &TestDb, ws: Workspace) -> Vec<NullRegression> {
    let mut all = workspace_regressions(db, ws);
    all.sort_by_key(ws_reg_key);
    all
}

fn canon_ws_reports(db: &TestDb, ws: Workspace) -> Vec<(NullRegression, usize)> {
    let mut reports: Vec<(NullRegression, usize)> = workspace_regressions_with_witnesses(db, ws)
        .into_iter()
        .map(|r| (r.regression, r.witness.len()))
        .collect();
    reports.sort_by_key(|(r, _)| ws_reg_key(r));
    reports
}

#[test]
fn workspace_incremental_matches_scratch() {
    let frags = fragments();
    let paths = ["f0.py", "f1.py", "f2.py", "f3.py"];

    for seed in 1..=6u64 {
        let mut rng = Rng(seed.wrapping_mul(0x2545F4914F6CDD1D).wrapping_add(7));

        // Incremental workspace: fixed file set, contents edited in place.
        let mut contents: Vec<usize> = paths.iter().map(|_| rng.below(frags.len())).collect();
        let mut inc_db = TestDb::default();
        let inc_files: Vec<(String, SourceFile)> = paths
            .iter()
            .zip(&contents)
            .map(|(p, &c)| (p.to_string(), SourceFile::new(&inc_db, frags[c].to_string())))
            .collect();
        let inc_handles: Vec<SourceFile> = inc_files.iter().map(|(_, f)| *f).collect();
        let inc_ws = Workspace::new(&inc_db, inc_files);

        for step in 0..50 {
            // Edit one file's contents.
            let fi = rng.below(paths.len());
            contents[fi] = rng.below(frags.len());
            inc_handles[fi].set_contents(&mut inc_db).to(frags[contents[fi]].to_string());

            // Build the same workspace from scratch.
            let scratch_db = TestDb::default();
            let scratch_files: Vec<(String, SourceFile)> = paths
                .iter()
                .zip(&contents)
                .map(|(p, &c)| (p.to_string(), SourceFile::new(&scratch_db, frags[c].to_string())))
                .collect();
            let scratch_ws = Workspace::new(&scratch_db, scratch_files);

            assert_eq!(
                workspace_summaries(&inc_db, inc_ws),
                workspace_summaries(&scratch_db, scratch_ws),
                "ws summaries diverged at seed {seed} step {step}"
            );
            assert_eq!(
                canon_ws_regressions(&inc_db, inc_ws),
                canon_ws_regressions(&scratch_db, scratch_ws),
                "ws regressions diverged at seed {seed} step {step}"
            );
            assert_eq!(
                canon_ws_reports(&inc_db, inc_ws),
                canon_ws_reports(&scratch_db, scratch_ws),
                "ws reports/witnesses diverged at seed {seed} step {step}"
            );
        }
    }
}

// ── Benign-edit stability: padding the file never changes the facts ────────────

#[test]
fn benign_padding_preserves_facts() {
    let base = "def buggy(x: Optional[str]):\n    return x.upper()\ndef clean():\n    return 1\n";

    let scratch_db = TestDb::default();
    let scratch_file = SourceFile::new(&scratch_db, base.to_string());
    let baseline = canon_regressions(&scratch_db, scratch_file);
    assert_eq!(baseline.len(), 1, "sanity: one regression in the base program");

    let mut db = TestDb::default();
    let file = SourceFile::new(&db, base.to_string());
    for pad in ["", "\n", "# c\n", "\n\n# x\n\n"] {
        let text = format!("{pad}{base}");
        file.set_contents(&mut db).to(text);
        let regs = canon_regressions(&db, file);
        assert_eq!(regs.len(), 1, "benign padding must not change the regression count");
        // Same function, same variable, same kind regardless of byte offset.
        assert_eq!(regs[0].func_name, baseline[0].func_name);
        assert_eq!(regs[0].object_symbol, baseline[0].object_symbol);
        assert_eq!(regs[0].kind, baseline[0].kind);
    }
}

// ── Property: the nullability lattice join is commutative, idempotent, and
//    collapses disagreement to Unknown (precision over soundness). ─────────────

#[test]
fn lattice_join_properties() {
    use NullabilityValue::{NonNull, Nullable, Unknown};
    let vals = [NonNull, Nullable, Unknown];
    for &a in &vals {
        assert_eq!(a.join(a), a, "idempotent");
        for &b in &vals {
            assert_eq!(a.join(b), b.join(a), "commutative");
            if a != b {
                assert_eq!(a.join(b), Unknown, "disagreement collapses to Unknown");
            }
        }
    }
}
