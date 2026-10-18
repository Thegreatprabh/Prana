"""Event types — the things that can wake PRANA up.

A chatbot only knows `user_message`. PRANA's runtime generates its own
prompts too: cron ticks, hook events, and background worker results all
become events that feed the same core loop. That is the architectural
difference between "prompt in, text out" and an agent with initiative.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Literal

EventKind = Literal["user_message", "cron_tick", "hook_event", "worker_result", "system"]


@dataclass
class Event:
    """One unit of work for the agent loop."""

    kind: EventKind
    # Human-readable instruction / message content for this event.
    text: str
    # Structured context: job id for cron_tick, file path for hook_event, etc.
    meta: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def context_line(self) -> str:
        """One-line context for the system prompt, e.g. '[cron_tick job=daily-digest]'."""
        if self.meta:
            bits = " ".join(f"{k}={v}" for k, v in self.meta.items())
            return f"[{self.kind} {bits}]"
        return f"[{self.kind}]"


def user_message(text: str, **meta: Any) -> Event:
    return Event(kind="user_message", text=text, meta=dict(meta))


def system(text: str, **meta: Any) -> Event:
    return Event(kind="system", text=text, meta=dict(meta))


def cron_tick(text: str, **meta: Any) -> Event:
    return Event(kind="cron_tick", text=text, meta=dict(meta))


def hook_event(text: str, **meta: Any) -> Event:
    return Event(kind="hook_event", text=text, meta=dict(meta))


def worker_result(text: str, **meta: Any) -> Event:
    return Event(kind="worker_result", text=text, meta=dict(meta))
