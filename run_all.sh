#!/usr/bin/env bash
# run_all.sh — full Rung 1 + Rung 2 + Phase 7 Part B pipeline
#
# Runs every automated step in order and writes all outputs to run_all_logs/.
# At the end it prints where each report file lives so results can be reviewed.
#
# Prerequisites:
#   export ANTHROPIC_API_KEY=sk-ant-...
#
# Usage:
#   bash run_all.sh                          # full run (100 instances, sonnet)
#   bash run_all.sh --n 20                   # use 20 instances (faster/cheaper)
#   bash run_all.sh --model claude-opus-4-7  # higher quality labels
#   bash run_all.sh --skip-loop              # skip Phase 7 Part B loop_eval
#   bash run_all.sh --skip-oracle            # skip Rung 2 oracle harness
#
# Cost estimate (100 instances, sonnet-4-6, with caching):
#   autolabel:   ~$1–2  (100 calls, cached system prompt)
#   autorecord:  ~$0.30 (analyzable subset only, ~30–50 calls)
#   loop_eval:   ~$1–3  (10 corpus cases × 2 arms × up to 5 iterations)
#   Total:       ~$3–7

set -uo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

N_INSTANCES=100
MODEL="claude-sonnet-4-6"       # oracle harness + loop_eval — must match Phase 8 agent model
LABEL_MODEL="claude-opus-4-7"   # autolabel — opus for judgment quality
RECORD_MODEL="claude-opus-4-7"  # autorecord — opus so the oracle feedback IS the true ceiling
WORKERS=4                       # parallel API workers (autolabel, autorecord, oracle harness)
SKIP_LOOP=false
SKIP_ORACLE=false
DAEMON_PATH="$SCRIPT_DIR/target/release/wake-daemon"
LOG_DIR="$SCRIPT_DIR/run_all_logs"
PYTHON=python3

# ── Argument parsing ──────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --n)             N_INSTANCES="$2"; shift 2 ;;
        --model)         MODEL="$2"; shift 2 ;;
        --label-model)   LABEL_MODEL="$2"; shift 2 ;;
        --record-model)  RECORD_MODEL="$2"; shift 2 ;;
        --workers)       WORKERS="$2"; shift 2 ;;
        --skip-loop)     SKIP_LOOP=true; shift ;;
        --skip-oracle)   SKIP_ORACLE=true; shift ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── Helpers ───────────────────────────────────────────────────────────────────

GREEN="\033[32m"
RED="\033[31m"
YELLOW="\033[33m"
BOLD="\033[1m"
RESET="\033[0m"

STEP=0
PASSED=()
FAILED=()
SKIPPED=()

step() {
    STEP=$((STEP + 1))
    echo
    echo -e "${BOLD}[Step $STEP] $*${RESET}"
}

ok()   { echo -e "  ${GREEN}OK${RESET}"; PASSED+=("$1"); }
fail() { echo -e "  ${RED}FAILED${RESET} — see $LOG_DIR/$2.log"; FAILED+=("$1"); }
skip() { echo -e "  ${YELLOW}SKIPPED${RESET}"; SKIPPED+=("$1"); }

run() {
    # run <step-name> <command...>
    # Tees stdout+stderr to log file; returns exit code.
    local name="$1"; shift
    local logfile="$LOG_DIR/${name}.log"
    # Use script -q for unbuffered tee on macOS; fall back to plain tee.
    if "$@" 2>&1 | tee "$logfile"; then
        return 0
    else
        return 1
    fi
}

# ── Prerequisites ─────────────────────────────────────────────────────────────

step "Checking prerequisites"

mkdir -p "$LOG_DIR"

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "  ERROR: ANTHROPIC_API_KEY is not set."
    echo "  Export it and retry:  export ANTHROPIC_API_KEY=sk-ant-..."
    exit 1
fi
echo "  ANTHROPIC_API_KEY: set"

# Build wake-daemon if not present.
if [[ ! -f "$DAEMON_PATH" ]]; then
    echo "  wake-daemon not found — building release binary..."
    if cargo build --release -p wake-daemon 2>&1 | tee "$LOG_DIR/cargo_build.log"; then
        echo "  wake-daemon: built"
    else
        echo "  ERROR: cargo build failed — see $LOG_DIR/cargo_build.log"
        exit 1
    fi
else
    echo "  wake-daemon: $DAEMON_PATH"
fi

# Check python packages.
for pkg in anthropic datasets; do
    if ! $PYTHON -c "import $pkg" 2>/dev/null; then
        echo "  Installing $pkg..."
        $PYTHON -m pip install "$pkg" -q
    fi
done
echo "  Python packages: OK"

echo
echo -e "  N_INSTANCES   = $N_INSTANCES"
echo -e "  LABEL_MODEL   = $LABEL_MODEL   (autolabel — failure-mode classification)"
echo -e "  RECORD_MODEL  = $RECORD_MODEL   (autorecord — oracle feedback writer; defines the ceiling)"
echo -e "  MODEL         = $MODEL   (oracle harness + loop_eval — must match Phase 8 agent)"
echo -e "  WORKERS       = $WORKERS  (parallelism for autolabel, autorecord, oracle harness)"
echo -e "  DAEMON        = $DAEMON_PATH"
echo -e "  LOGS          = $LOG_DIR"
echo -e "  SKIP_LOOP     = $SKIP_LOOP"
echo -e "  SKIP_ORACLE   = $SKIP_ORACLE"

# ── Step 1: Collect ───────────────────────────────────────────────────────────

step "Collect $N_INSTANCES instances from SWE-bench Verified"

if run "collect" \
    $PYTHON probe/audit/collect.py \
        --source gold \
        --n "$N_INSTANCES"; then
    ok "collect"
else
    fail "collect" "collect"
    echo "  Cannot continue without a dataset."
    exit 1
fi

# ── Step 2: Autolabel smoke-test ──────────────────────────────────────────────

step "Autolabel smoke-test (1 record — verifies API connectivity)"

if run "autolabel_smoke" \
    $PYTHON probe/audit/autolabel.py \
        --model "$LABEL_MODEL" \
        --smoke-test; then
    ok "autolabel_smoke"
else
    fail "autolabel_smoke" "autolabel_smoke"
    echo "  API connectivity check failed — check your ANTHROPIC_API_KEY and model access."
    exit 1
fi

# ── Step 3: Autolabel full run ────────────────────────────────────────────────

step "Autolabel all unlabeled records (model: $LABEL_MODEL)"

if run "autolabel" \
    $PYTHON probe/audit/autolabel.py \
        --model "$LABEL_MODEL" \
        --workers "$WORKERS"; then
    ok "autolabel"
else
    # Non-fatal: partial labeling is still usable.
    fail "autolabel" "autolabel"
    echo "  Partial labeling — continuing with what was labeled."
fi

# ── Step 4: Rung 1 analysis ───────────────────────────────────────────────────

step "Rung 1 analysis — bucket breakdown and kill/redirect signals"

if run "analyze" \
    $PYTHON probe/audit/analyze.py; then
    ok "analyze"
    echo
    echo "  Rung 1 report:"
    cat probe/audit/reports/rung1_report.txt | sed 's/^/    /'
else
    fail "analyze" "analyze"
fi

# ── Step 5: Auto-generate oracle feedback ────────────────────────────────────

if [[ "$SKIP_ORACLE" == "true" ]]; then
    step "Oracle feedback generation (SKIPPED — --skip-oracle)"
    skip "autorecord"
else
    step "Generate oracle feedback for analyzable instances (model: $RECORD_MODEL)"

    if run "autorecord" \
        $PYTHON probe/oracle/autorecord.py \
            --model "$RECORD_MODEL" \
            --workers "$WORKERS"; then
        ok "autorecord"
    else
        fail "autorecord" "autorecord"
        echo "  Oracle feedback generation failed — Rung 2 will be skipped."
        SKIP_ORACLE=true
    fi
fi

# ── Step 6: Oracle harness ────────────────────────────────────────────────────

if [[ "$SKIP_ORACLE" == "true" ]]; then
    step "Oracle harness (SKIPPED)"
    skip "oracle_harness"
else
    step "Rung 2 oracle harness — oracle vs ablation (model: $MODEL)"

    if run "oracle_harness" \
        $PYTHON probe/oracle/harness.py \
            --all \
            --model "$MODEL" \
            --workers "$WORKERS"; then
        ok "oracle_harness"
    else
        fail "oracle_harness" "oracle_harness"
        echo "  Oracle harness had errors — eval will show partial results."
    fi
fi

# ── Step 7: Rung 2 evaluation ─────────────────────────────────────────────────

if [[ "$SKIP_ORACLE" == "true" ]]; then
    step "Rung 2 eval (SKIPPED)"
    skip "oracle_eval"
else
    step "Rung 2 evaluation — ceiling delta"

    if run "oracle_eval" \
        $PYTHON probe/oracle/eval.py; then
        ok "oracle_eval"
        echo
        echo "  Rung 2 report:"
        cat probe/oracle/reports/rung2_report.txt | sed 's/^/    /'
    else
        fail "oracle_eval" "oracle_eval"
    fi
fi

# ── Step 8: Phase 7 Part B — loop_eval ───────────────────────────────────────

if [[ "$SKIP_LOOP" == "true" ]]; then
    step "Phase 7 Part B loop_eval (SKIPPED — --skip-loop)"
    skip "loop_eval"
else
    step "Phase 7 Part B — agent loop + ablation on corpus (model: $MODEL)"

    if run "loop_eval" \
        $PYTHON harness/agent-loop/loop_eval.py \
            --daemon "$DAEMON_PATH" \
            --model "$MODEL"; then
        ok "loop_eval"
    else
        fail "loop_eval" "loop_eval"
        echo "  Loop eval had errors — see log for details."
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  RUN COMPLETE"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

if [[ ${#PASSED[@]} -gt 0 ]]; then
    echo -e "  ${GREEN}Passed:${RESET}"
    for s in "${PASSED[@]}"; do echo "    ✓  $s"; done
fi
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo -e "  ${RED}Failed:${RESET}"
    for s in "${FAILED[@]}"; do echo "    ✗  $s"; done
fi
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
    echo -e "  ${YELLOW}Skipped:${RESET}"
    for s in "${SKIPPED[@]}"; do echo "    -  $s"; done
fi

echo
echo "  Output files for review:"
echo "  ─────────────────────────────────────────────────────────────────"

list_if_exists() {
    local path="$1"
    local label="$2"
    if [[ -f "$path" ]]; then
        echo "    $label"
        echo "      $path"
    fi
}

list_if_exists "probe/audit/corpus/labeled_failures.jsonl" "Labeled dataset"
list_if_exists "probe/audit/reports/collect_summary.txt"   "Collection summary"
list_if_exists "probe/audit/reports/autolabel_log.json"    "Autolabel session log"
list_if_exists "probe/audit/reports/autolabel_summary.tsv" "Autolabel TSV (all labels)"
list_if_exists "probe/audit/reports/rung1_report.txt"      "Rung 1 report  ← KEY DECISION"
list_if_exists "probe/oracle/reports/rung2_report.txt"     "Rung 2 report  ← CEILING DELTA"
list_if_exists "$LOG_DIR/loop_eval.log"                    "Phase 7 Part B log"

echo
echo "  Step logs: $LOG_DIR/"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Exit non-zero if any step failed.
if [[ ${#FAILED[@]} -gt 0 ]]; then
    exit 1
fi
