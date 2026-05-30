"""
wake_harness — CEGIS loop that gates model edits through the wake daemon.

The model edits files; the harness runs wake analysis after each attempt; if
the edit introduces regressions the harness formats the findings as natural-
language feedback and re-prompts. The model never sees the wake API.

All three daemon methods are used:
  workspace/didChange  — register/update files
  analyze/regressions  — show the agent what issues exist before it starts
  analyze/blastRadius  — gate each candidate edit (does not commit)
  query/valueFlow      — enrich feedback with def-use provenance
"""
from __future__ import annotations

import re
import sys
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "clients" / "wake-py"))
from wake_client import WakeClient


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    success: bool
    iterations: int
    final_text: str
    regressions_caught: list[dict]  # the last blast showing regressions (empty on success)
    latency_ms: list[float]         # per-iteration wake latency


@dataclass
class HarnessConfig:
    daemon_path: str = "wake-daemon"
    model: str = "claude-sonnet-4-6"
    budget: int = 5
    ablation: bool = False  # if True, replace wake feedback with a dumb retry prompt


# ---------------------------------------------------------------------------
# Wake feedback formatting
# ---------------------------------------------------------------------------

def byte_to_line(text: str, offset: int) -> int:
    return text[: max(0, offset)].count("\n") + 1


def format_witness(witness: list[dict]) -> str:
    parts = []
    for step in witness:
        k = step.get("kind", "?")
        if k == "none_assignment":
            parts.append(f"None assigned to '{step['symbol']}'")
        elif k == "nullable_param":
            parts.append(f"param '{step['symbol']}' is Optional (may be None)")
        elif k == "variable_copy":
            parts.append(f"'{step['from']}' (Nullable) copied to '{step['to']}'")
        elif k == "call_return":
            parts.append(f"'{step['to']}' assigned from call to {step['callee']}() which can return None")
        elif k == "consumer":
            sym, ck = step["symbol"], step.get("consumer_kind", "dereference")
            parts.append(f"'{sym}' dereferenced ({ck})")
        elif k == "opaque":
            parts.append(f"(source partially unknown: {step['symbol']})")
    return " → ".join(parts) if parts else "(no trace)"


def format_regressions(regressions: list[dict], source_text: str) -> str:
    if not regressions:
        return "No nullability regressions found."
    lines = []
    for r in regressions:
        conf = r.get("confidence", "?")
        rc = r.get("root_cause", {})
        rc_kind = rc.get("kind", "unknown")
        rc_sym = rc.get("symbol", "?")
        if rc_kind == "none_assignment":
            src_desc = f"'{rc_sym}' is directly assigned None"
        elif rc_kind == "nullable_param":
            src_desc = f"param '{rc_sym}' is annotated Optional (can be None)"
        else:
            src_desc = rc.get("description", "unknown source")

        lines.append(f"[{conf.upper()}] Root cause: {src_desc}")
        for c in r.get("consumers", []):
            br = c.get("byte_range", [0, 0])
            ln = byte_to_line(source_text, br[0])
            ck = c.get("kind", "dereference")
            sym = c.get("symbol", "?")
            trace = format_witness(c.get("witness", []))
            lines.append(f"  • line {ln}: '{sym}' used as {ck} — {trace}")
        if fl := r.get("fix_locus"):
            ln = byte_to_line(source_text, fl[0])
            lines.append(f"  Fix at: line {ln}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_value_flow(flows: list[list[int]], source_text: str, label: str) -> str:
    if not flows:
        return ""
    locs = [f"line {byte_to_line(source_text, b[0])}" for b in flows[:5]]
    return f"  Def-use chain for '{label}': {', '.join(locs)}"


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_python_block(text: str) -> str | None:
    """Extract the first ```python...``` or ```...``` block from model output."""
    m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Core CEGIS loop
# ---------------------------------------------------------------------------

class WakeHarness:
    def __init__(self, cfg: HarnessConfig):
        self.cfg = cfg
        self.anthropic = anthropic.Anthropic()

    def _build_feedback(
        self,
        regs: list[dict],
        source_text: str,
        client: "WakeClient",
        uri: str,
        preamble: str = (
            "Your edit introduced the following potential None-dereferences "
            "(confirmed by static analysis):"
        ),
    ) -> str:
        """Format regression findings into a feedback message for the model."""
        if self.cfg.ablation:
            return (
                "That version has issues. Please try a different approach "
                "and return the complete file in a ```python ... ``` block."
            )
        # Enrich each regression's consumers with def-use provenance.
        extra_flow: list[str] = []
        for reg in regs:
            for consumer in reg.get("consumers", []):
                br = consumer.get("byte_range", [0, 0])
                sym = consumer.get("symbol", "")
                flows = client.query_value_flow(uri, br[0], direction="backward")
                line = format_value_flow(flows, source_text, sym)
                if line:
                    extra_flow.append(line)

        reg_block = format_regressions(regs, source_text)
        msg = f"{preamble}\n\n{reg_block}"
        if extra_flow:
            msg += "\nDef-use provenance:\n" + "\n".join(extra_flow)
        msg += "\n\nFix these issues and return the complete updated file in a ```python ... ``` block."
        return msg

    def run(
        self,
        files: dict[str, str],   # uri → source text (all workspace files)
        primary_uri: str,         # the file the agent should edit
        task: str,
    ) -> LoopResult:
        """
        Run the CEGIS loop.

        files:       {uri: text} for every file in the workspace.
        primary_uri: which file the agent edits.
        task:        natural-language description of what to fix.
        """
        with WakeClient(self.cfg.daemon_path) as client:
            # Register all files in the workspace.
            for uri, text in files.items():
                client.did_change(uri, text)

            primary_text = files[primary_uri]
            latencies: list[float] = []

            # --- Retrieval mode: show the agent what issues exist before it starts ---
            t0 = time.perf_counter()
            initial_regs = client.analyze_regressions(primary_uri)
            latencies.append((time.perf_counter() - t0) * 1000)

            # Use query/valueFlow to enrich context at each regression's consumer site.
            flow_context_lines: list[str] = []
            for reg in initial_regs:
                for consumer in reg.get("consumers", []):
                    br = consumer.get("byte_range", [0, 0])
                    sym = consumer.get("symbol", "")
                    flows = client.query_value_flow(primary_uri, br[0], direction="backward")
                    line = format_value_flow(flows, primary_text, sym)
                    if line:
                        flow_context_lines.append(line)

            flow_context = "\n".join(flow_context_lines)

            # --- Build the initial message ---
            issue_block = format_regressions(initial_regs, primary_text)
            primary_filename = primary_uri.split("/")[-1]

            system = (
                "You are a precise Python engineer. You will be given a task and the "
                "contents of a Python file. Return ONLY the complete corrected file "
                "inside a single ```python ... ``` code block with no other text."
            )
            initial_user = (
                f"Task: {task}\n\n"
                f"File: {primary_filename}\n"
                f"```python\n{primary_text}```\n\n"
            )
            if initial_regs:
                initial_user += (
                    f"Current nullability analysis findings:\n{issue_block}\n"
                )
                if flow_context:
                    initial_user += f"\nDef-use provenance:\n{flow_context}\n"
            initial_user += (
                "\nReturn the complete corrected file in a ```python ... ``` block."
            )

            messages: list[dict] = [{"role": "user", "content": initial_user}]
            regressions_caught: list[dict] = []

            for iteration in range(self.cfg.budget):
                # Call the model.  Per-call timeout caps any single hung
                # request; the SDK still does its internal 429/529 retries
                # underneath.
                response = self.anthropic.messages.create(
                    model=self.cfg.model,
                    max_tokens=4096,
                    system=system,
                    messages=messages,
                    timeout=180,
                )
                # Find the first text block — avoid IndexError when an
                # interleaved thinking block sits at content[0].
                assistant_text = next(
                    (b.text for b in response.content if b.type == "text"), ""
                )
                new_text = extract_python_block(assistant_text)

                if new_text is None:
                    # Model didn't return a code block; nudge it.
                    messages.append({"role": "assistant", "content": assistant_text})
                    messages.append({
                        "role": "user",
                        "content": "Please return the complete file inside a ```python ... ``` code block.",
                    })
                    continue

                # --- Verification mode: two-stage gate ---
                #
                # Stage 1 — blastRadius: does the edit *introduce* new regressions?
                # blastRadius diffs the currently-committed state against new_text
                # WITHOUT committing, so "before" = original buggy file.
                t0 = time.perf_counter()
                blast = client.analyze_blast_radius(primary_uri, new_text)
                latencies.append((time.perf_counter() - t0) * 1000)

                new_regs = blast.get("new_regressions", [])

                if new_regs:
                    # Edit introduced regressions — build feedback and loop.
                    regressions_caught = new_regs
                    feedback = self._build_feedback(
                        new_regs, new_text, client, primary_uri
                    )
                    messages.append({"role": "assistant", "content": assistant_text})
                    messages.append({"role": "user", "content": feedback})
                    continue

                # Stage 2 — commit the proposed edit and check what remains.
                # The edit doesn't introduce new bugs; now check whether it
                # actually *fixed* the original ones. Without this check the
                # loop could declare success on a no-op edit that neither
                # introduces nor fixes anything.
                client.did_change(primary_uri, new_text)

                t0 = time.perf_counter()
                remaining_regs = client.analyze_regressions(primary_uri)
                latencies.append((time.perf_counter() - t0) * 1000)

                if not remaining_regs:
                    # All issues resolved — true success.
                    return LoopResult(
                        success=True,
                        iterations=iteration + 1,
                        final_text=new_text,
                        regressions_caught=[],
                        latency_ms=latencies,
                    )

                # Original issues remain — feed them back and continue.
                regressions_caught = remaining_regs
                feedback = self._build_feedback(
                    remaining_regs, new_text, client, primary_uri,
                    preamble=(
                        "Your edit doesn't introduce new issues, but the following "
                        "problems from the original code are still present:"
                    ),
                )
                messages.append({"role": "assistant", "content": assistant_text})
                messages.append({"role": "user", "content": feedback})

            # Budget exhausted.
            # new_text may already be committed (if Stage 2 ran on the last
            # iteration); the daemon is idempotent on re-registration.
            if new_text:
                client.did_change(primary_uri, new_text)
                final = new_text
            else:
                final = primary_text

            return LoopResult(
                success=False,
                iterations=self.cfg.budget,
                final_text=final,
                regressions_caught=regressions_caught,
                latency_ms=latencies,
            )
