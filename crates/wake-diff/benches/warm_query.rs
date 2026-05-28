/// Latency benchmark for warm-query incremental analysis.
///
/// Measures two things (per §6.9 of design.md):
///   1. Cold-start cost: first `regressions_with_witnesses` on a fresh db.
///   2. Warm-edit cost: second call after a small localized edit.
///
/// The warm cost should be roughly proportional to the change, not the file.
/// If it's not, incrementality is broken.
use criterion::{criterion_group, criterion_main, BenchmarkId, Criterion};
use wake_diff::regressions_with_witnesses;
use wake_engine::{Database, Db, SourceFile};

// A realistic Python file with multiple functions and cross-function None flow.
const BENCH_SRC: &str = r#"
def get_value(flag):
    if flag:
        return None
    return "ok"

def process(flag):
    v = get_value(flag)
    return v.upper()

def helper(x):
    return x.strip()

def top(flag):
    result = get_value(flag)
    result.strip()
    helper(result)
    return result
"#;

// A small localized edit: rename one function body variable, no semantic change.
const BENCH_SRC_EDITED: &str = r#"
def get_value(flag):
    if flag:
        return None
    return "ok"

def process(flag):
    val = get_value(flag)
    return val.upper()

def helper(x):
    return x.strip()

def top(flag):
    result = get_value(flag)
    result.strip()
    helper(result)
    return result
"#;

fn bench_warm_query(c: &mut Criterion) {
    let mut group = c.benchmark_group("warm_query");

    // Cold-start: fresh database, first analysis.
    group.bench_function(BenchmarkId::new("cold_start", "regressions"), |b| {
        b.iter(|| {
            let db = Database::default();
            let file = SourceFile::new(&db, BENCH_SRC.to_string());
            regressions_with_witnesses(&db, file)
        });
    });

    // Warm-edit: same database, one small edit, then re-analyze.
    group.bench_function(BenchmarkId::new("warm_edit", "regressions"), |b| {
        // Setup: pre-warm the database.
        let mut db = Database::default();
        let file = SourceFile::new(&db, BENCH_SRC.to_string());
        let _ = regressions_with_witnesses(&db, file);

        b.iter(|| {
            // Simulate an edit.
            file.set_contents(&mut db).to(BENCH_SRC_EDITED.to_string());
            let r = regressions_with_witnesses(&db, file);
            // Restore so next iteration starts from the same warm state.
            file.set_contents(&mut db).to(BENCH_SRC.to_string());
            let _ = regressions_with_witnesses(&db, file);
            r
        });
    });

    group.finish();
}

criterion_group!(benches, bench_warm_query);
criterion_main!(benches);
