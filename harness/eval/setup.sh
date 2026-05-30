#!/usr/bin/env bash
# Phase 8 setup: install SWE-agent, SWE-bench, and verify Docker.
# Run once from the repo root: bash harness/eval/setup.sh
set -euo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$EVAL_DIR/../.." && pwd)"

# Pick the right package-install command for the active env:
#   uv pip     — when uv is installed (preferred; activates the venv automatically)
#   pip3       — POSIX-y venvs that expose pip3 but not pip
#   pip        — classic name
# All branches end up with the SAME effective installer; the only difference is
# the binary entry point so the script runs regardless of the user's setup.
if command -v uv &>/dev/null; then
  PIP_INSTALL="uv pip install"
  PIP_NAME="uv pip"
elif command -v pip3 &>/dev/null; then
  PIP_INSTALL="pip3 install"
  PIP_NAME="pip3"
elif command -v pip &>/dev/null; then
  PIP_INSTALL="pip install"
  PIP_NAME="pip"
else
  echo "ERROR: no Python package installer found (need one of: uv, pip3, pip)."
  exit 1
fi

echo "=== Wake Phase 8 setup ==="
echo "Installer: $PIP_NAME"

# 1. Docker
echo "Checking Docker..."
if ! docker info &>/dev/null; then
  echo "ERROR: Docker daemon is not running. Start Docker and retry."
  exit 1
fi
echo "  Docker: OK"

# 2. Python deps
# uv manages its own resolver so we don't need to upgrade pip/setuptools when
# uv is in use; classic pip needs the bump so pyproject-only installs work.
if [ "$PIP_NAME" != "uv pip" ]; then
  echo "Upgrading pip + setuptools..."
  $PIP_INSTALL -q --upgrade pip setuptools wheel
fi
echo "Installing Python dependencies..."
$PIP_INSTALL -q -r "$EVAL_DIR/requirements.txt"
echo "  Python deps: OK"

# 3. SWE-agent
# Install EDITABLY.  SWE-agent v1.x has import-time assertions for sibling
# config/, tools/, trajectories/ directories — these only resolve when the
# installed package's __file__ points back at the source tree.  A non-editable
# install severs that link and produces "AssertionError: ...site-packages/config"
# at first import.  uv reliably supports PEP 660 editable installs; older plain
# pip can fail here, hence the upgrade above.
SWE_AGENT_DIR="$EVAL_DIR/swe-agent"
if [ ! -d "$SWE_AGENT_DIR" ]; then
  echo "Cloning SWE-agent..."
  git clone --depth 1 https://github.com/princeton-nlp/SWE-agent.git "$SWE_AGENT_DIR"
fi
echo "Installing SWE-agent (editable)..."
$PIP_INSTALL -q -e "$SWE_AGENT_DIR"
echo "  SWE-agent: installed"

# 4. SWE-bench (for dataset + test harness)
echo "Installing SWE-bench..."
$PIP_INSTALL -q swebench
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
