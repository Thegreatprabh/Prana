"""Shell tool: run a command with timeout inside a sandboxed working dir.

`exec` is classified WRITE (it changes local state). The working directory
defaults to the sandbox root and any `cwd` argument is resolved *inside* it —
`..` escapes are rejected. Output is captured (never streamed to the
terminal raw) so the loop can truncate it before feeding it back.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List

from .base import SideEffect, Tool

DEFAULT_TIMEOUT_S = 60
MAX_OUTPUT_CHARS = 20000  # hard cap before the loop's own 4000-char truncation


def _resolve_cwd(sandbox_root: Path, cwd: str | None) -> Path:
    base = sandbox_root.resolve()
    target = (base / cwd).resolve() if cwd else base
    # Reject escapes: target must stay within the sandbox.
    if target != base and base not in target.parents:
        raise ValueError(f"cwd escapes sandbox: {cwd}")
    if not target.is_dir():
        raise ValueError(f"cwd is not a directory: {cwd}")
    return target


def make_exec(sandbox_root: str | None = None):
    root = Path(sandbox_root or os.getcwd())

    def exec(cmd: str, timeout: int = DEFAULT_TIMEOUT_S, cwd: str | None = None) -> str:
        """Run `cmd` via the shell. Returns combined stdout+stderr and exit code."""
        workdir = _resolve_cwd(root, cwd)
        timeout = max(1, min(int(timeout), 600))
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout}s: {cmd[:200]}"
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > MAX_OUTPUT_CHARS:
            out = out[:MAX_OUTPUT_CHARS] + f"\n… [output capped at {MAX_OUTPUT_CHARS} chars]"
        return f"$ {cmd}\n(exit {proc.returncode})\n{out}".strip()

    return exec


def builtin_shell_tools(sandbox_root: str | None = None) -> List[Tool]:
    return [
        Tool(
            name="exec",
            description=(
                "Run a shell command. Working directory defaults to the project "
                "directory; `cwd` must stay inside it. Has a timeout (default 60s, max 600s). "
                "Returns combined stdout/stderr plus the exit code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (1-600)"},
                    "cwd": {"type": "string", "description": "Relative working dir inside the sandbox"},
                },
                "required": ["cmd"],
            },
            side_effect=SideEffect.WRITE,
            func=make_exec(sandbox_root),
        )
    ]
