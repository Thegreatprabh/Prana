"""Append-only daily log: memory/daily/YYYY-MM-DD.md.

Each entry is timestamped. The log is write-only from the agent's
perspective — a background summarizer (P3+) promotes durable facts up to
MEMORY.md. Never edited in place.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .store import memory_root


def daily_dir() -> Path:
    d = memory_root() / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _today_path() -> Path:
    return daily_dir() / f"{datetime.now(timezone.utc).date().isoformat()}.md"


def log_today(entry: str) -> str:
    """Append one timestamped entry to today's log. Returns confirmation."""
    entry = (entry or "").strip()
    if not entry:
        return "nothing logged — empty entry ignored"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    with _today_path().open("a", encoding="utf-8") as fh:
        fh.write(f"- {ts} — {entry}\n")
    return "logged"
