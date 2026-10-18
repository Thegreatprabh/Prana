"""The delivery decision — the heart of "thoughtful" proactivity.

SPEC.md §2.4: notify the user only when something is genuinely new and
worth their attention. This module is where that judgment lives:

- ``should_deliver(job, result_text)`` — pure decision, no side effects.
- ``deliver(target, text)`` — pluggable sinks (console now, Telegram in P4).

Decision rules (in order):
1. Empty/whitespace-only results are NEVER delivered.
2. A result the agent explicitly marked ``[NOTIFY]`` / ``[URGENT]`` on its
   first line is ALWAYS delivered (the daemon instructs the model to use
   this marker only for genuinely interrupt-worthy outcomes).
3. Otherwise: deliver when the result differs meaningfully from the last
   one (sha256 compare against ``job.last_result_hash``). First run with
   content always delivers. Unchanged results stay silent.

``job`` is duck-typed: anything with ``.last_result_hash`` works, so hooks
and one-shot worker results reuse the same decision.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Dict

NOTIFY_MARKERS = ("[NOTIFY]", "[URGENT]")


class DeliveryError(Exception):
    """Raised when a delivery target has no registered sink."""


def result_hash(text: str) -> str:
    """Stable sha256 of a result text (stripped)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def strip_marker(text: str) -> str:
    """Remove a leading [NOTIFY]/[URGENT] marker line before delivery."""
    lines = (text or "").splitlines()
    if lines and lines[0].strip() in NOTIFY_MARKERS:
        return "\n".join(lines[1:]).lstrip("\n")
    return text or ""


def should_deliver(job, result_text: str | None) -> bool:
    """Decide whether `result_text` is worth sending. No side effects."""
    text = (result_text or "").strip()
    if not text:
        return False  # rule 1: never deliver emptiness
    first_line = text.split("\n", 1)[0].strip()
    if first_line in NOTIFY_MARKERS:
        return True  # rule 2: the agent explicitly asked to interrupt
    last_hash = getattr(job, "last_result_hash", None)
    if not last_hash:
        return True  # rule 3a: first run with content always delivers
    return result_hash(text) != last_hash  # rule 3b: only real change


#: Sink functions: (arg, text) -> None. `arg` is the part after "kind:".
SinkFn = Callable[[str, str], None]

_sinks: Dict[str, SinkFn] = {}


def register_sink(kind: str, fn: SinkFn) -> None:
    """Register a delivery sink. P4 registers "telegram"; tests use fakes."""
    if not kind or not kind.strip():
        raise ValueError("sink kind must not be empty")
    if not callable(fn):
        raise ValueError("sink must be callable")
    _sinks[kind.strip().lower()] = fn


def _console_sink(arg: str, text: str) -> None:
    print(text)


register_sink("console", _console_sink)


def deliver(target: str, text: str) -> None:
    """Deliver `text` to `target` like "console" or "telegram:CHAT_ID".

    Raises DeliveryError when no sink is registered for the target kind.
    """
    if not target or not target.strip():
        raise DeliveryError("empty delivery target")
    kind, _, arg = target.strip().partition(":")
    sink = _sinks.get(kind.strip().lower())
    if sink is None:
        # Never include the full target: it can carry PII (phone numbers).
        raise DeliveryError(
            f"no sink registered for channel {kind.strip().lower()!r} "
            f"(known: {sorted(_sinks)})"
        )
    sink(arg, text)


__all__ = [
    "NOTIFY_MARKERS",
    "DeliveryError",
    "result_hash",
    "strip_marker",
    "should_deliver",
    "register_sink",
    "deliver",
]
