"""
WakeHook — AbstractAgentHook subclass that gates SWE-agent edits through
the wake daemon.

Integration points used:
  on_init          — store agent reference, configure paths
  on_setup_done    — cold-start: register all repo Python files with daemon
  on_action_executed — detect file changes via `git diff`, run blastRadius,
                       inject regression feedback into the observation
  on_run_done      — stop daemon, save per-task findings log

The gate is mandatory: regression feedback is appended directly to the step
observation so the agent sees it and must respond before taking another action.
The model never calls wake directly; it receives natural-language feedback as
part of its environment output.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# SWE-agent imports — graceful stub if not installed (for unit testing)
try:
    from sweagent.agent.hooks.abstract import AbstractAgentHook
except ImportError:
    class AbstractAgentHook:  # type: ignore[no-redef]
        def on_init(self, *, agent: Any) -> None: ...
        def on_run_start(self) -> None: ...
        def on_setup_done(self) -> None: ...
        def on_action_executed(self, *, step: Any) -> None: ...
        def on_run_done(self, *, trajectory: Any, info: Any) -> None: ...

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "clients" / "wake-py"))
from wake_client import WakeClient, RpcError

# ---------------------------------------------------------------------------
# Finding record (written to per-task JSON log for metrics.py)
# ---------------------------------------------------------------------------

@dataclass
class WakeFinding:
    step_index: int
    changed_files: list[str]
    new_regressions: list[dict]
    fixed_regressions: list[dict]
    latency_ms: float


@dataclass
class TaskLog:
    instance_id: str
    arm: str              # "wake" or "ablation"
    findings: list[WakeFinding] = field(default_factory=list)
    cold_start_ms: float = 0.0
    total_wake_ms: float = 0.0
    files_registered: int = 0


# ---------------------------------------------------------------------------
# Feedback formatting (same helpers as wake_harness.py, inline for portability)
# ---------------------------------------------------------------------------

def _byte_to_line(text: str, offset: int) -> int:
    return text[: max(0, offset)].count("\n") + 1


def _format_witness(witness: list[dict]) -> str:
    parts = []
    for step in witness:
        k = step.get("kind", "?")
        if k == "none_assignment":
            parts.append(f"None assigned to '{step['symbol']}'")
        elif k == "nullable_param":
            parts.append(f"param '{step['symbol']}' is Optional (may be None)")
        elif k == "variable_copy":
            parts.append(f"'{step['from']}' copied to '{step['to']}' (Nullable)")
        elif k == "call_return":
            parts.append(f"'{step['to']}' = {step['callee']}() which can return None")
        elif k == "consumer":
            parts.append(f"'{step['symbol']}' dereferenced ({step.get('consumer_kind','?')})")
        elif k == "opaque":
            parts.append(f"(partial trace: {step['symbol']})")
    return " → ".join(parts) if parts else "(no trace)"


def _format_regressions(regressions: list[dict], file_contents: dict[str, str]) -> str:
    lines = ["WAKE STATIC ANALYSIS — potential None-dereferences introduced by this edit:\n"]
    for r in regressions:
        conf = r.get("confidence", "?").upper()
        rc = r.get("root_cause", {})
        rc_kind = rc.get("kind", "?")
        if rc_kind == "none_assignment":
            src = f"'{rc['symbol']}' directly assigned None"
        elif rc_kind == "nullable_param":
            src = f"param '{rc['symbol']}' is Optional (can be None)"
        else:
            src = rc.get("description", "unknown")
        lines.append(f"[{conf}] Root cause: {src}")

        for c in r.get("consumers", []):
            br = c.get("byte_range", [0, 0])
            sym = c.get("symbol", "?")
            ck = c.get("kind", "?")
            # Find which file this consumer is in
            file_text = next(iter(file_contents.values()), "")
            ln = _byte_to_line(file_text, br[0])
            trace = _format_witness(c.get("witness", []))
            lines.append(f"  • line {ln}: '{sym}' used as {ck} — {trace}")

        if fl := r.get("fix_locus"):
            file_text = next(iter(file_contents.values()), "")
            ln = _byte_to_line(file_text, fl[0])
            lines.append(f"  Suggested fix location: line {ln}")
        lines.append("")

    lines.append("Fix these issues before proceeding. Each dereference of a None value will raise an AttributeError, TypeError, or KeyError at runtime.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# WakeHook
# ---------------------------------------------------------------------------

class WakeHook(AbstractAgentHook):
    """
    Mandatory wake gate for SWE-agent.

    Constructor args:
      daemon_path: path to the wake-daemon binary
      output_dir:  directory to write per-task JSON logs
      arm:         "wake" (full feedback) or "ablation" (hook active but no feedback injected)
      instance_id: SWE-bench instance ID, for logging
    """

    def __init__(
        self,
        daemon_path: str,
        output_dir: str,
        arm: str = "wake",
        instance_id: str = "unknown",
    ) -> None:
        self.daemon_path = daemon_path
        self.output_dir = Path(output_dir)
        self.arm = arm
        self.instance_id = instance_id

        self._agent: Any = None
        self._client: WakeClient | None = None
        self._daemon_proc: subprocess.Popen | None = None
        self._log = TaskLog(instance_id=instance_id, arm=arm)
        self._step_index = 0
        self._last_diff_set: set[str] = set()
        self._registered_uris: set[str] = set()

    # ── SWE-agent hook callbacks ──────────────────────────────────────────────

    def on_init(self, *, agent: Any) -> None:
        self._agent = agent

    def on_setup_done(self) -> None:
        """
        Called after the SWE-agent environment is set up and the repo is
        accessible inside Docker. This is the cold-start: walk the repo,
        register all Python files with the daemon.
        """
        t0 = time.perf_counter()
        self._start_daemon()

        py_files = self._list_repo_python_files()
        for fpath in py_files:
            try:
                content = self._read_file(fpath)
                uri = f"file://{fpath}"
                self._client.did_change(uri, content)
                self._registered_uris.add(uri)
            except Exception:
                pass  # unreadable file — skip silently

        self._log.cold_start_ms = (time.perf_counter() - t0) * 1000
        self._log.files_registered = len(self._registered_uris)

    def on_action_executed(self, *, step: Any) -> None:
        """
        Called after each agent action. Detects file edits via git diff,
        re-registers changed files, runs blastRadius, and injects wake
        feedback into the observation if regressions are found.
        """
        if self._client is None:
            return

        self._step_index += 1
        t0 = time.perf_counter()

        # Detect changed Python files since last step using git diff.
        changed = self._get_changed_py_files()
        if not changed:
            return

        # Re-read and re-register each changed file.
        file_contents: dict[str, str] = {}
        for fpath in changed:
            try:
                content = self._read_file(fpath)
                uri = f"file://{fpath}"
                self._client.did_change(uri, content)
                self._registered_uris.add(uri)
                file_contents[uri] = content
            except Exception:
                pass

        if not file_contents:
            return

        # Run blastRadius on each changed file (non-committing preview).
        all_new: list[dict] = []
        all_fixed: list[dict] = []
        for uri, content in file_contents.items():
            try:
                blast = self._client.analyze_blast_radius(uri, content)
                all_new.extend(blast.get("new_regressions", []))
                all_fixed.extend(blast.get("fixed_regressions", []))
            except RpcError:
                pass

        latency_ms = (time.perf_counter() - t0) * 1000
        self._log.total_wake_ms += latency_ms

        if all_new or all_fixed:
            self._log.findings.append(WakeFinding(
                step_index=self._step_index,
                changed_files=list(changed),
                new_regressions=all_new,
                fixed_regressions=all_fixed,
                latency_ms=latency_ms,
            ))

        # Mandatory gate: inject feedback into observation if regressions found.
        if all_new and self.arm == "wake":
            feedback = _format_regressions(all_new, file_contents)
            self._inject_observation(step, feedback)

    def on_run_done(self, *, trajectory: Any, info: Any) -> None:
        """Stop daemon and write per-task JSON log."""
        self._stop_daemon()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.output_dir / f"{self.instance_id}_{self.arm}.json"
        with open(log_path, "w") as f:
            json.dump({
                "instance_id": self._log.instance_id,
                "arm": self._log.arm,
                "cold_start_ms": self._log.cold_start_ms,
                "total_wake_ms": self._log.total_wake_ms,
                "files_registered": self._log.files_registered,
                "findings": [
                    {
                        "step": fi.step_index,
                        "changed_files": fi.changed_files,
                        "new_regressions": fi.new_regressions,
                        "fixed_regressions": fi.fixed_regressions,
                        "latency_ms": fi.latency_ms,
                    }
                    for fi in self._log.findings
                ],
            }, f, indent=2)

    # ── Daemon lifecycle ──────────────────────────────────────────────────────

    def _start_daemon(self) -> None:
        self._daemon_proc = subprocess.Popen(
            [self.daemon_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._client = WakeClient.__new__(WakeClient)
        self._client._proc = self._daemon_proc
        self._client._req_id = 0
        import threading
        self._client._lock = threading.Lock()

    def _stop_daemon(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        if self._daemon_proc:
            try:
                self._daemon_proc.terminate()
                self._daemon_proc.wait(timeout=5)
            except Exception:
                pass

    # ── Environment helpers ───────────────────────────────────────────────────

    def _communicate(self, cmd: str) -> str:
        """Run a bash command inside the SWE-agent Docker container."""
        try:
            output, _ = self._agent.env.communicate(cmd)
            return output.strip()
        except Exception:
            return ""

    def _list_repo_python_files(self) -> list[str]:
        """Return absolute paths of all Python files in the repo (inside container)."""
        out = self._communicate(
            "find /root -name '*.py' -not -path '*/.*' -not -path '*/node_modules/*' "
            "-not -path '*/__pycache__/*' 2>/dev/null | head -500"
        )
        return [p for p in out.splitlines() if p.endswith(".py")]

    def _get_changed_py_files(self) -> list[str]:
        """
        Return Python files changed since the last git commit (or since the
        last call). Uses `git diff --name-only HEAD` inside the container.
        """
        out = self._communicate(
            "git -C /root/$(ls /root | head -1) diff --name-only HEAD 2>/dev/null "
            "| grep '\\.py$'"
        )
        if not out:
            return []
        # Construct absolute paths
        repo_dir = self._communicate("ls /root | head -1").strip()
        return [f"/root/{repo_dir}/{p}" for p in out.splitlines() if p]

    def _read_file(self, fpath: str) -> str:
        """Read a file from inside the Docker container."""
        out = self._communicate(f"cat {fpath} 2>/dev/null")
        return out

    # ── Observation injection ─────────────────────────────────────────────────

    def _inject_observation(self, step: Any, feedback: str) -> None:
        """
        Append wake feedback to the step observation so the agent sees it.
        Tries direct attribute mutation first; falls back to object.__setattr__
        for frozen dataclasses.
        """
        separator = "\n\n" + "─" * 60 + "\n"
        try:
            step.observation = (step.observation or "") + separator + feedback
        except (AttributeError, TypeError):
            try:
                object.__setattr__(step, "observation",
                                   (step.observation or "") + separator + feedback)
            except Exception:
                # Last resort: log the finding but can't inject
                pass
