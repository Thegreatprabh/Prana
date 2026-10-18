"""PRANA long-term memory (SPEC.md §2.3).

Markdown files under ``~/.prana/memory/`` (overridable via PRANA_MEMORY_DIR
for tests): MEMORY.md (curated facts), people/<name>.md (per-person notes),
daily/YYYY-MM-DD.md (append-only log).

Write policy (SPEC.md §2.3, §5.2): durable facts only — NEVER credentials.
Any write that looks like it contains a key is refused, not scrubbed.
"""

from . import daily, people, recall, store

__all__ = ["daily", "people", "recall", "store"]
