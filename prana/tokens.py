"""Token-frugal utilities.

The loop MUST truncate any tool output over ~4000 chars before feeding it
back to the model (SPEC.md). These helpers keep context small without
losing the signal: head/tail truncation for outputs, and history
compaction that preserves the system prompt plus recent turns.
"""

from __future__ import annotations

from typing import Dict, List

#: Hard ceiling for a single tool observation fed back to the model.
MAX_TOOL_OUTPUT_CHARS = 4000

#: Rough chars-per-token for estimation (English-ish text).
CHARS_PER_TOKEN = 4


import re

#: Patterns that look like API keys — scrubbed from any surfaced error text.
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"sk-[A-Za-z0-9_\-]+"),
    re.compile(r"AIza[A-Za-z0-9_\-]+"),
    re.compile(r"xox[bap]-[A-Za-z0-9\-]+"),
]


def scrub_secrets(text: str) -> str:
    """Best-effort removal of key-like strings. Defense in depth — providers
    must still redact their own keys; this catches anything that slips through."""
    if not text:
        return text
    for pat in _KEY_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def estimate_tokens(text: str) -> int:
    """Rough token estimate. Never exact — only used for budgeting, not billing."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def truncate_output(text: str, max_chars: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Truncate long tool output, keeping head and tail with an ellipsis marker.

    Head keeps the beginning (often the most informative part of listings and
    logs); tail keeps the end (exit codes, tracebacks' final frames).
    """
    if len(text) <= max_chars:
        return text
    # Reserve room for the marker line.
    marker = "\n… [truncated {n} chars] …\n"
    # Split budget: 60% head, 40% tail.
    tail_budget = int(max_chars * 0.4)
    head_budget = max_chars - tail_budget - len(marker.format(n=0)) - 8
    head = text[:head_budget]
    tail = text[-tail_budget:] if tail_budget > 0 else ""
    omitted = len(text) - len(head) - len(tail)
    return head + marker.format(n=omitted) + tail


def compact_history(
    messages: List[Dict[str, str]],
    max_tokens: int = 12000,
) -> List[Dict[str, str]]:
    """Compact a message history to fit a token budget.

    Always preserves the system message and the most recent turns.
    Dropped middle turns are replaced by a single summary placeholder so the
    model knows context was elided rather than silently vanishing.
    """
    total = sum(estimate_tokens(m.get("content", "")) for m in messages)
    if total <= max_tokens:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    kept: List[Dict[str, str]] = []
    budget = max_tokens - sum(estimate_tokens(m.get("content", "")) for m in system_msgs)
    dropped = 0
    # Walk from the most recent backwards, keeping what fits.
    for m in reversed(rest):
        cost = estimate_tokens(m.get("content", ""))
        if cost <= budget:
            kept.append(m)
            budget -= cost
        else:
            dropped += 1
    kept.reverse()

    placeholder: List[Dict[str, str]] = []
    if dropped:
        placeholder = [
            {
                "role": "user",
                "content": f"[… {dropped} earlier turn(s) compacted to save context …]",
            }
        ]
    return system_msgs + placeholder + kept
