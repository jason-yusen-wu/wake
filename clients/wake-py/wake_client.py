"""
wake_client — thin Python client for the wake-daemon JSON-RPC server.

Usage:
    from wake_client import WakeClient

    client = WakeClient("/path/to/wake-daemon")
    client.did_change("file:///project/foo.py", open("foo.py").read())
    regressions = client.analyze_regressions("file:///project/foo.py")
    for r in regressions:
        print(r["root_cause"], r["confidence"])

Protocol: JSON-RPC 2.0 over stdin/stdout, one request per line (newline-delimited).
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message


class WakeClient:
    """Synchronous client for the wake-daemon process.

    Spawns the daemon on construction and communicates over stdio.
    All public methods are thread-safe.
    """

    def __init__(self, daemon_path: str | Path = "wake-daemon") -> None:
        self._proc = subprocess.Popen(
            [str(daemon_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._req_id = 0
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def did_change(self, uri: str, text: str) -> None:
        """Register or update a file's contents in the daemon workspace."""
        self._call("workspace/didChange", {"uri": uri, "text": text})

    def analyze_regressions(self, uri: str) -> list[dict[str, Any]]:
        """Return shaped regression feedback for the given file URI.

        Each item has:
          root_cause: {kind, symbol, byte_range}
          consumers:  [{symbol, kind, byte_range, witness}]
          confidence: "high" | "medium" | "low"
          fix_locus:  [start, end] | null
        """
        result = self._call("analyze/regressions", {"uri": uri})
        return result.get("regressions", [])

    def analyze_blast_radius(self, uri: str, new_text: str) -> dict[str, Any]:
        """Preview the effect of replacing uri's content with new_text.

        Returns:
          blast_radius:      [[start, end], ...]   — nodes whose status changed
          new_regressions:   [shaped feedback, ...]
          fixed_regressions: [{symbol, consumer}, ...]

        The daemon database is NOT updated; use did_change() to commit.
        """
        return self._call("analyze/blastRadius", {"uri": uri, "text": new_text})

    def query_value_flow(
        self, uri: str, position: int, direction: str = "both"
    ) -> list[list[int]]:
        """Retrieval mode: def-use related nodes for the symbol at `position`.

        direction:
          "backward" — definitions reaching the use at `position`
          "forward"  — uses reached by the definition at `position`
          "both"     — the union (default)

        Returns a list of [start_byte, end_byte] node ranges.
        """
        result = self._call(
            "query/valueFlow",
            {"uri": uri, "position": position, "direction": direction},
        )
        return result.get("nodes", [])

    def close(self) -> None:
        """Shut down the daemon process."""
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait()

    def __enter__(self) -> "WakeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            req_id = self._next_id()
            req = json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
            )
            assert self._proc.stdin is not None
            assert self._proc.stdout is not None
            self._proc.stdin.write(req + "\n")
            self._proc.stdin.flush()
            resp_line = self._proc.stdout.readline()

        if not resp_line:
            raise ConnectionError("daemon process closed its stdout")

        resp = json.loads(resp_line)
        if "error" in resp:
            err = resp["error"]
            raise RpcError(err["code"], err["message"])
        return resp.get("result")

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id
