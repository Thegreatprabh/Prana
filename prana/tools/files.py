"""File tools: read, write, edit (exact-match), list.

All paths are resolved inside the sandbox root — `..` escapes and absolute
paths outside the sandbox are rejected. `read`/`list` are READ; `write` and
`edit` are WRITE. `edit` requires the old text to match exactly once, so the
model can't silently rewrite the wrong block.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .base import SideEffect, Tool

MAX_READ_CHARS = 60000  # hard cap; the loop truncates further to ~4000


def _resolve(root: Path, path: str) -> Path:
    base = root.resolve()
    # Treat the path as relative to the sandbox even if it looks absolute.
    candidate = (base / path.lstrip("/")).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"path escapes sandbox: {path}")
    return candidate


def make_tools(sandbox_root: str | None = None):
    root = Path(sandbox_root or os.getcwd())

    def read(path: str) -> str:
        """Read a text file. Returns its contents (capped)."""
        target = _resolve(root, path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n… [file capped at {MAX_READ_CHARS} chars]"
        return text

    def write(path: str, content: str) -> str:
        """Write (create or overwrite) a text file. Creates parent dirs."""
        target = _resolve(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {path}"

    def edit(path: str, old_text: str, new_text: str) -> str:
        """Replace `old_text` with `new_text`. old_text must match exactly once."""
        target = _resolve(root, path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_text)
        if count == 0:
            raise ValueError("old_text not found (must match exactly)")
        if count > 1:
            raise ValueError(f"old_text matches {count} times — be more specific")
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"edited {path}: replaced {len(old_text)} chars"

    def list(path: str = ".") -> str:  # noqa: A002 - tool arg name is part of the API
        """List a directory (non-recursive, names only)."""
        target = _resolve(root, path)
        if not target.is_dir():
            raise ValueError(f"not a directory: {path}")
        names = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return "\n".join(names) if names else "(empty)"

    return read, write, edit, list


def builtin_file_tools(sandbox_root: str | None = None) -> List[Tool]:
    read, write, edit, list_dir = make_tools(sandbox_root)
    return [
        Tool(
            name="read",
            description="Read a text file inside the project. Returns contents (capped).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative file path"}},
                "required": ["path"],
            },
            side_effect=SideEffect.READ,
            func=read,
        ),
        Tool(
            name="write",
            description="Create or overwrite a text file inside the project. Creates parent dirs.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
            side_effect=SideEffect.WRITE,
            func=write,
        ),
        Tool(
            name="edit",
            description=(
                "Replace old_text with new_text in a file. old_text must match "
                "EXACTLY once, otherwise the edit is rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "old_text": {"type": "string", "description": "Exact text to find"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            side_effect=SideEffect.WRITE,
            func=edit,
        ),
        Tool(
            name="list",
            description="List a directory inside the project (names only, non-recursive).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative dir path (default .)"}},
                "required": [],
            },
            side_effect=SideEffect.READ,
            func=list_dir,
        ),
    ]
