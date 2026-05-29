"""
swe_agent_wrapper — subprocess entry point that attaches WakeHook to
SWE-agent before running.

Invoked by task_runner._run_via_subprocess when the Python API path is
unavailable. Reads WAKE_* environment variables set by task_runner.

Hook attachment strategy:
  1. Try the agent_callback kwarg accepted by recent SWE-agent versions.
  2. Fall back to patching DefaultAgent.run (works on older versions that
     support add_hook but not agent_callback).
  3. If neither works, raise clearly rather than silently running without
     the hook.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

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


def _attach(agent: object) -> None:
    """Attach the WakeHook to a SWE-agent agent object."""
    if not hasattr(agent, "add_hook"):
        raise RuntimeError(
            "SWE-agent DefaultAgent has no add_hook method. "
            "Check your SWE-agent version — this harness requires add_hook support."
        )
    agent.add_hook(hook)


# ── Strategy 1: agent_callback kwarg (SWE-agent >= recent version) ────────────
try:
    from sweagent.run.run_single import main as swe_main

    # Probe whether swe_main accepts agent_callback.  Call with a sentinel
    # that will fail fast on the real arguments if the kwarg is not supported,
    # rather than silently ignoring it.
    import inspect
    _sig = inspect.signature(swe_main)
    if "agent_callback" in _sig.parameters:
        swe_main(sys.argv[1:], agent_callback=_attach)
    else:
        raise TypeError("agent_callback not in signature")

except TypeError:
    # ── Strategy 2: monkey-patch DefaultAgent.run ─────────────────────────────
    try:
        from sweagent.agent.agents import DefaultAgent
        from sweagent.run.run_single import main as swe_main

        _orig_run = DefaultAgent.run

        def _patched_run(self: DefaultAgent, *args: object, **kwargs: object) -> object:
            self.add_hook(hook)
            return _orig_run(self, *args, **kwargs)

        DefaultAgent.run = _patched_run  # type: ignore[method-assign]
        swe_main(sys.argv[1:])

    except ImportError as exc:
        raise RuntimeError(
            f"Could not import SWE-agent to attach WakeHook: {exc}. "
            "Run setup.sh first."
        ) from exc
