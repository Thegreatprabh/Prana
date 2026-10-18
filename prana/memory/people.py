"""Per-person notes: memory/people/<name>.md.

Names are sanitized to safe filenames — no path traversal is possible even
if the model passes something hostile like "../../etc".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from .store import memory_root

_PEOPLE_DIR = "people"
_MAX_NAME_LEN = 64


def people_dir() -> Path:
    d = memory_root() / _PEOPLE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    """Sanitize a person name into a safe filename stem.

    Keeps letters, digits, spaces, hyphens, underscores; everything else
    becomes an underscore. Rejects empty / dot-only results.
    """
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "_", (name or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:_MAX_NAME_LEN].strip()
    if not cleaned or cleaned in (".", "..") or set(cleaned) <= {"."}:
        raise ValueError(f"unusable person name: {name!r}")
    return cleaned


def _path(name: str) -> Path:
    # Belt and braces: resolve and confirm containment even after sanitizing.
    p = (people_dir() / f"{safe_name(name)}.md").resolve()
    if people_dir().resolve() not in p.parents:
        raise ValueError(f"unsafe person name: {name!r}")
    return p


def get_person(name: str) -> str:
    """Notes for a person; empty string when none exist."""
    try:
        return _path(name).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def save_person(name: str, text: str) -> str:
    """Write (replace) a person's notes. Returns confirmation."""
    path = _path(name)  # raises ValueError on bad names
    path.write_text((text or "").strip() + "\n", encoding="utf-8")
    return f"saved notes for {safe_name(name)}"


def list_people() -> List[str]:
    """Names (filename stems) of everyone with notes, sorted."""
    try:
        return sorted(p.stem for p in people_dir().glob("*.md") if p.is_file())
    except OSError:
        return []
