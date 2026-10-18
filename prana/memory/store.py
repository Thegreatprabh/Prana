"""Curated long-term memory: MEMORY.md.

Facts are short dated lines, e.g. ``- 2026-10-18: Prabh prefers Hinglish.``
The file is created silently on first use (zero config).

Write policy: any text that matches a credential pattern is REFUSED — the
write does not happen and a warning is returned. We never scrub-and-store,
because a half-redacted secret is still a leak vector.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

from .. import config, tokens

MEMORY_FILE_NAME = "MEMORY.md"
ENV_OVERRIDE = "PRANA_MEMORY_DIR"

_HEADER = "# PRANA long-term memory\n\n"


def memory_root() -> Path:
    """Root of the memory tree. Env override exists for tests only."""
    override = os.environ.get(ENV_OVERRIDE)
    root = Path(override).expanduser() if override else config.get_config_dir() / "memory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path() -> Path:
    return memory_root() / MEMORY_FILE_NAME


def _contains_credential(text: str) -> bool:
    """True when scrub_secrets would change the text (i.e. it held a secret)."""
    return tokens.scrub_secrets(text) != text


def read_memory() -> str:
    """Full MEMORY.md text; empty string when missing or blank."""
    try:
        return _path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def append_fact(fact: str) -> str:
    """Append one dated fact line. Refuses credential-like content.

    Returns a human-readable confirmation or refusal (safe to show the model).
    """
    fact = (fact or "").strip()
    if not fact:
        return "nothing to remember — empty fact ignored"
    if _contains_credential(fact):
        print("memory: refused write — looks like a credential", file=sys.stderr)
        return "refused: that looks like a credential — I never store secrets"
    line = f"- {date.today().isoformat()}: {fact}\n"
    path = _path()
    if not path.exists():
        path.write_text(_HEADER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return f"remembered: {fact}"


def update_memory(full_text: str) -> str:
    """Replace MEMORY.md wholesale (curating). Same credential refusal."""
    full_text = (full_text or "").strip()
    if _contains_credential(full_text):
        print("memory: refused overwrite — looks like a credential", file=sys.stderr)
        return "refused: that looks like a credential — I never store secrets"
    path = _path()
    text = full_text if full_text.endswith("\n") or not full_text else full_text + "\n"
    path.write_text((_HEADER if not full_text.startswith("#") else "") + text,
                    encoding="utf-8")
    return "memory updated"
