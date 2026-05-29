#!/usr/bin/env bash
# Phase 8 setup: install SWE-agent, SWE-bench, and verify Docker.
# Run once from the repo root: bash harness/eval/setup.sh
set -euo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$EVAL_DIR/../.." && pwd)"

echo "=== Wake Phase 8 setup ==="

# 1. Docker
echo "Checking Docker..."
if ! docker info &>/dev/null; then
  echo "ERROR: Docker daemon is not running. Start Docker and retry."
  exit 1
fi
echo "  Docker: OK"

# 2. Python deps
echo "Installing Python dependencies..."
pip install -q -r "$EVAL_DIR/requirements.txt"
echo "  Python deps: OK"

# 3. SWE-agent
SWE_AGENT_DIR="$EVAL_DIR/swe-agent"
if [ ! -d "$SWE_AGENT_DIR" ]; then
  echo "Cloning SWE-agent..."
  git clone --depth 1 https://github.com/princeton-nlp/SWE-agent.git "$SWE_AGENT_DIR"
  pip install -q -e "$SWE_AGENT_DIR[dev]"
  echo "  SWE-agent: cloned and installed"
else
  echo "  SWE-agent: already present at $SWE_AGENT_DIR"
fi

# 4. SWE-bench (for dataset + test harness)
echo "Installing SWE-bench..."
pip install -q swebench
echo "  SWE-bench: OK"

# 5. Pull the SWE-bench Verified Docker image (used for test execution)
echo "Pulling SWE-bench evaluation image (this may take a few minutes)..."
docker pull sweagent/swe-agent:latest 2>/dev/null || true
echo "  Docker image: OK"

# 6. Wake daemon binary
DAEMON="$ROOT_DIR/target/release/wake-daemon"
if [ ! -f "$DAEMON" ]; then
  echo "Building wake-daemon (release)..."
  cargo build --release -p wake-daemon --manifest-path "$ROOT_DIR/Cargo.toml"
  echo "  wake-daemon: built at $DAEMON"
else
  echo "  wake-daemon: already built at $DAEMON"
fi

echo ""
echo "=== Setup complete ==="
echo "Next: python harness/eval/task_runner.py --instance-id <id> --dataset swebench_verified"
