"""
WakeHook — AbstractAgentHook subclass that gates SWE-agent edits through
the wake daemon.

Integration points used:
  on_init          — store agent reference, configure paths
  on_setup_done    — cold-start: register all repo Python files with daemon
                     and take the initial per-file regression snapshot
  on_action_executed — detect file changes via `git diff`, re-register them,
                       compare regressions against the per-file snapshot, and
                       inject feedback into the observation when new regressions
                       are found
  on_run_done      — stop daemon, save per-task findings log

Ordering guarantee (why the snapshot approach is correct):
  blastRadius(uri, text) diffs the *currently committed* DB state against
  `text`.  If we call didChange(uri, text) first then blastRadius(uri, text),
  before==after and the diff is always empty — the gate never fires.  Instead
  we commit ALL changed files first (required so cross-file callee changes are
  visible to the workspace summary), then compare analyze_regressions output
  against a snapshot taken at the end of the previous step.  New regressions
  that appear since the snapshot are exactly what the current action introduced.
"""
from __future__ import annotations

import hashlib
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
    # Status tracking — distinguishes "wake silent because no bug found"
    # from "WakeHook crashed before it could analyze anything".
    setup_complete: bool = False        # True after on_setup_done succeeds
    cold_start_error: str = ""          # populated on any setup failure
    rpc_errors: int = 0                 # count of failed daemon RPCs at runtime
    last_rpc_error: str = ""


# ---------------------------------------------------------------------------
# Feedback formatting
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
            parts.append(f"param '{step['symbol']}' is Optional (can be None)")
        elif k == "variable_copy":
            parts.append(f"'{step['from']}' copied to '{step['to']}' (Nullable)")
        elif k == "call_return":
            parts.append(f"'{step['to']}' = {step['callee']}() which can return None")
        elif k == "consumer":
            parts.append(f"'{step['symbol']}' dereferenced ({step.get('consumer_kind','?')})")
        elif k == "opaque":
            parts.append(f"(partial trace: {step['symbol']})")
    return " → ".join(parts) if parts else "(no trace)"


def _resolve_text(file_uri: str | None, file_contents: dict[str, str]) -> str:
    """Look up text by URI; fall back to any registered file if URI missing."""
    if file_uri and file_uri in file_contents:
        return file_contents[file_uri]
    return next(iter(file_contents.values()), "")


def _format_regressions(regressions: list[dict], file_contents: dict[str, str]) -> str:
    """
    Each regression is expected to carry a ``__file_uri`` key set by the hook
    when it aggregated cross-file results.  All byte_ranges within that
    regression (root cause, consumers, fix locus) refer to that single file.
    Without the tag we fall back to any registered .py text — line numbers
    may be wrong but the message is still informative.
    """
    lines = ["WAKE STATIC ANALYSIS — potential None-dereferences introduced by this edit:\n"]
    for r in regressions:
        home_uri = r.get("__file_uri")
        home_text = _resolve_text(home_uri, file_contents)
        home_short = home_uri.rsplit("/", 1)[-1] if home_uri else ""

        conf = r.get("confidence", "?").upper()
        rc = r.get("root_cause", {})
        rc_kind = rc.get("kind", "?")
        if rc_kind == "none_assignment":
            src = f"'{rc['symbol']}' directly assigned None"
        elif rc_kind == "nullable_param":
            src = f"param '{rc['symbol']}' is Optional (can be None)"
        else:
            src = rc.get("description", "unknown")
        in_file = f" in {home_short}" if home_short else ""
        lines.append(f"[{conf}] Root cause: {src}{in_file}")

        for c in r.get("consumers", []):
            br = c.get("byte_range", [0, 0])
            sym = c.get("symbol", "?")
            ck = c.get("kind", "?")
            ln = _byte_to_line(home_text, br[0])
            trace = _format_witness(c.get("witness", []))
            lines.append(f"  • line {ln}: '{sym}' used as {ck} — {trace}")

        if fl := r.get("fix_locus"):
            ln = _byte_to_line(home_text, fl[0])
            lines.append(f"  Suggested fix location: line {ln}")
        lines.append("")

    lines.append(
        "Fix these issues before proceeding. "
        "Each dereference of a None value will raise AttributeError, TypeError, or KeyError at runtime."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Regression snapshot helpers
# ---------------------------------------------------------------------------

def _regression_key(r: dict) -> tuple:
    """Stable identity for a shaped regression finding (root-cause + first consumer)."""
    rc = r.get("root_cause", {})
    consumers = r.get("consumers", [])
    first = consumers[0] if consumers else {}
    return (
        rc.get("kind", ""),
        rc.get("symbol", ""),
        first.get("symbol", ""),
        first.get("kind", ""),
    )


def _new_regressions(prev: list[dict], curr: list[dict]) -> list[dict]:
    """Return regressions that appear in curr but not in prev."""
    prev_keys = {_regression_key(r) for r in prev}
    return [r for r in curr if _regression_key(r) not in prev_keys]


def _fixed_regressions(prev: list[dict], curr: list[dict]) -> list[dict]:
    """Return regressions that were in prev but are gone from curr."""
    curr_keys = {_regression_key(r) for r in curr}
    return [r for r in prev if _regression_key(r) not in curr_keys]


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# WakeHook
# ---------------------------------------------------------------------------

class WakeHook(AbstractAgentHook):
    """
    Mandatory wake gate for SWE-agent.

    Constructor args:
      daemon_path: path to the wake-daemon binary
      output_dir:  directory to write per-task JSON logs
      arm:         "wake" (full feedback) or "ablation" (hook active, no feedback injected)
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

        # Per-file state across steps.
        # uri → content hash of the last version we registered & snapshotted
        self._registered_hashes: dict[str, str] = {}
        # uri → shaped regressions at the END of the previous step (or setup)
        self._reg_snapshots: dict[str, list[dict]] = {}
        # Cached repo directory (detected in on_setup_done)
        self._repo_dir: str = ""

    # ── SWE-agent hook callbacks ──────────────────────────────────────────────

    def on_init(self, *, agent: Any) -> None:
        self._agent = agent

    def on_setup_done(self) -> None:
        """
        Called after the SWE-agent environment is set up and the repo is
        accessible inside Docker. Cold-start: walk the repo, register all
        Python files with the daemon, and take the initial regression snapshot.

        All failures here are caught and recorded on self._log.cold_start_error
        so downstream metrics can distinguish "wake silent (no bug)" from
        "wake crashed at setup".
        """
        t0 = time.perf_counter()
        try:
            self._start_daemon()
            # Detect and cache the repo directory once.
            self._repo_dir = self._detect_repo_dir()

            py_files = self._list_repo_python_files()
            for fpath in py_files:
                try:
                    content = self._read_file(fpath)
                    uri = f"file://{fpath}"
                    self._client.did_change(uri, content)
                    self._registered_hashes[uri] = _content_hash(content)
                except Exception:
                    # Unreadable individual file is fine — skip but keep going.
                    pass

            self._log.cold_start_ms = (time.perf_counter() - t0) * 1000
            self._log.files_registered = len(self._registered_hashes)

            # Take the initial per-file regression snapshot (baseline).
            for uri in list(self._registered_hashes):
                try:
                    self._reg_snapshots[uri] = self._client.analyze_regressions(uri)
                except RpcError as exc:
                    self._reg_snapshots[uri] = []
                    self._log.rpc_errors += 1
                    self._log.last_rpc_error = str(exc)[:200]
            self._log.setup_complete = True
        except Exception as exc:
            # Whole cold-start failed — record so we don't silently treat
            # missing findings as "clean run".
            self._log.cold_start_error = f"{type(exc).__name__}: {exc}"[:300]
            self._log.cold_start_ms = (time.perf_counter() - t0) * 1000

    def on_action_executed(self, *, step: Any) -> None:
        """
        Called after each agent action. Detects file edits via git diff,
        re-registers changed files, compares regressions against the per-file
        snapshot, and injects feedback into the observation if new regressions
        are found.

        Ordering: ALL changed files are committed via didChange first so that
        cross-file callee changes are visible to workspace_summaries before we
        query regressions.  We then diff against the snapshot rather than using
        blastRadius, which avoids the commit-then-preview self-cancellation bug.
        """
        if self._client is None:
            return

        self._step_index += 1
        t0 = time.perf_counter()

        # Detect Python files changed since the last step (incremental diff).
        changed_fpaths = self._get_changed_py_files()
        if not changed_fpaths:
            return

        # Read new contents and filter to files that actually changed.
        new_contents: dict[str, str] = {}  # uri → new text
        for fpath in changed_fpaths:
            try:
                content = self._read_file(fpath)
                uri = f"file://{fpath}"
                new_hash = _content_hash(content)
                if self._registered_hashes.get(uri) == new_hash:
                    continue  # content identical to last registered version — skip
                new_contents[uri] = content
                self._registered_hashes[uri] = new_hash
            except Exception:
                pass

        if not new_contents:
            return

        # Commit ALL changed files first (cross-file summaries depend on callee updates).
        for uri, content in new_contents.items():
            try:
                self._client.did_change(uri, content)
                # Ensure any newly-seen file gets a baseline snapshot of [] before
                # we snapshot it below.
                if uri not in self._reg_snapshots:
                    self._reg_snapshots[uri] = []
            except RpcError:
                pass

        # Now query current regressions and diff against per-file snapshots.
        # Tag each regression with the URI it came from so multi-file
        # aggregation doesn't lose track of which file each finding lives in.
        all_new: list[dict] = []
        all_fixed: list[dict] = []
        for uri in new_contents:
            try:
                curr = self._client.analyze_regressions(uri)
            except RpcError:
                continue
            prev = self._reg_snapshots.get(uri, [])
            new_regs = _new_regressions(prev, curr)
            fixed_regs = _fixed_regressions(prev, curr)
            for r in new_regs:
                r["__file_uri"] = uri
            for r in fixed_regs:
                r["__file_uri"] = uri
            all_new.extend(new_regs)
            all_fixed.extend(fixed_regs)
            # Update snapshot so the next step diffs against this state.
            self._reg_snapshots[uri] = curr

        latency_ms = (time.perf_counter() - t0) * 1000
        self._log.total_wake_ms += latency_ms

        if all_new or all_fixed:
            self._log.findings.append(WakeFinding(
                step_index=self._step_index,
                changed_files=list(new_contents.keys()),
                new_regressions=all_new,
                fixed_regressions=all_fixed,
                latency_ms=latency_ms,
            ))

        # Mandatory gate: inject feedback into observation if new regressions found.
        if all_new and self.arm == "wake":
            feedback = _format_regressions(all_new, new_contents)
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
                "setup_complete": self._log.setup_complete,
                "cold_start_error": self._log.cold_start_error,
                "rpc_errors": self._log.rpc_errors,
                "last_rpc_error": self._log.last_rpc_error,
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
        proc = subprocess.Popen(
            [self.daemon_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._daemon_proc = proc
        self._client = WakeClient.from_process(proc)

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
        """
        Run a bash command inside the SWE-agent Docker container.

        v1.1.0 notes:
          - env lives on agent._env (underscore-prefix), not agent.env
          - SWEEnv.communicate returns a single str (not a (stdout, rc) tuple)
        Both differ from the v0.x API the previous WakeHook was written for.
        Failures bump rpc_errors so downstream metrics can detect them.
        """
        env = getattr(self._agent, "_env", None)
        if env is None:
            self._log.rpc_errors += 1
            self._log.last_rpc_error = "agent._env is None (setup not complete?)"
            return ""
        try:
            output = env.communicate(cmd)
            return output.strip() if isinstance(output, str) else ""
        except Exception as exc:
            self._log.rpc_errors += 1
            self._log.last_rpc_error = f"{type(exc).__name__}: {exc}"[:200]
            return ""

    def _detect_repo_dir(self) -> str:
        """
        Return the absolute path to the repository root inside the container.
        SWE-agent always clones the repo under /root/<repo_name>.  We use
        `git rev-parse --show-toplevel` which is unambiguous, falling back to
        `ls /root` if git is unavailable for some reason.
        """
        path = self._communicate("git rev-parse --show-toplevel 2>/dev/null")
        if path:
            return path
        # Fallback: take the first directory under /root that looks like a repo.
        entries = self._communicate("ls -1 /root 2>/dev/null").splitlines()
        for entry in entries:
            candidate = f"/root/{entry.strip()}"
            if self._communicate(f"test -d {candidate}/.git && echo yes") == "yes":
                return candidate
        # Last resort: first entry (original fragile behaviour, but now isolated here).
        first = entries[0].strip() if entries else ""
        return f"/root/{first}" if first else "/root"

    def _list_repo_python_files(self) -> list[str]:
        """Return absolute paths of all Python files in the repo (inside container)."""
        repo = self._repo_dir or "/root"
        out = self._communicate(
            f"find {repo} -name '*.py' -not -path '*/.*' -not -path '*/node_modules/*' "
            f"-not -path '*/__pycache__/*' 2>/dev/null | head -500"
        )
        return [p for p in out.splitlines() if p.endswith(".py")]

    def _get_changed_py_files(self) -> list[str]:
        """
        Return Python files changed since the last git commit (HEAD).
        Uses the cached repo path so we don't re-run ls/git each step.
        """
        repo = self._repo_dir
        if not repo:
            return []
        out = self._communicate(
            f"git -C {repo} diff --name-only HEAD 2>/dev/null | grep '\\.py$'"
        )
        if not out:
            return []
        return [f"{repo}/{p}" for p in out.splitlines() if p]

    def _read_file(self, fpath: str) -> str:
        """
        Read a file from inside the Docker container.
        v1.1.0 exposes SWEEnv.read_file(path) which handles binary detection,
        encoding, and large files better than `cat`.  Fall back to cat for
        older builds.
        """
        env = getattr(self._agent, "_env", None)
        if env is not None and hasattr(env, "read_file"):
            try:
                return env.read_file(fpath)
            except Exception as exc:
                self._log.rpc_errors += 1
                self._log.last_rpc_error = f"read_file({fpath}): {exc}"[:200]
                return ""
        # Quote the path to handle spaces; cat is safe here (no user-controlled
        # interpolation beyond what the repo already controls).
        return self._communicate(f"cat '{fpath}' 2>/dev/null")

    # ── Observation injection ─────────────────────────────────────────────────

    def _inject_observation(self, step: Any, feedback: str) -> None:
        """
        Append wake feedback to the step observation so the agent sees it.
        Tries direct attribute mutation first; falls back to object.__setattr__
        for frozen dataclasses.
        """
        separator = "\n\n" + "─" * 60 + "\n"
        new_obs = (getattr(step, "observation", None) or "") + separator + feedback
        try:
            step.observation = new_obs
        except (AttributeError, TypeError):
            try:
                object.__setattr__(step, "observation", new_obs)
            except Exception:
                pass  # can't inject — finding is still logged
