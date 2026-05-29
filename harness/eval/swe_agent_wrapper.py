"""
swe_agent_wrapper — subprocess entry point that attaches WakeHook to
SWE-agent before running.

Invoked by task_runner._run_via_subprocess when the Python API path is
unavailable. Reads WAKE_* environment variables set by task_runner.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add harness/eval to path so wake_hook is importable
sys.path.insert(0, str(Path(__file__).parent))

from wake_hook import WakeHook

daemon_path = os.environ["WAKE_DAEMON_PATH"]
arm = os.environ["WAKE_ARM"]
instance_id = os.environ["WAKE_INSTANCE_ID"]
output_dir = os.environ["WAKE_OUTPUT_DIR"]

hook = WakeHook(
    daemon_path=daemon_path,
    output_dir=output_dir,
    arm=arm,
    instance_id=instance_id,
)

# Import and run SWE-agent with the hook attached.
# SWE-agent's run_single accepts a pre-run callback in recent versions.
try:
    from sweagent.run.run_single import main as swe_main, RunSingleConfig
    import yaml, argparse

    # Parse remaining args as SWE-agent args
    remaining = sys.argv[1:]

    # Try to attach hook via the agent_callback mechanism
    def _attach(agent):
        agent.add_hook(hook)

    swe_main(remaining, agent_callback=_attach)

except TypeError:
    # Older SWE-agent versions without agent_callback — monkey-patch add_hook
    from sweagent.agent.agents import DefaultAgent

    _orig_run = DefaultAgent.run

    def _patched_run(self, *args, **kwargs):
        self.add_hook(hook)
        return _orig_run(self, *args, **kwargs)

    DefaultAgent.run = _patched_run

    from sweagent.run.run_single import main as swe_main
    swe_main(sys.argv[1:])
