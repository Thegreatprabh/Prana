"""Memory tools for the registry: `remember` and `recall`.

- `remember` (WRITE): appends a durable fact to long-term memory. Not
  irreversible, so no approval prompt — but the store enforces the
  credential write policy itself.
- `recall` (READ): searches stored memory for relevant snippets.
"""

from __future__ import annotations

from typing import List

from ..memory import recall as _recall
from ..memory import store as _store
from .base import SideEffect, Tool


def _remember(fact: str) -> str:
    return _store.append_fact(fact)


def _recall_tool(query: str, k: int = 5) -> str:
    try:
        k = max(1, min(int(k), 10))
    except (TypeError, ValueError):
        k = 5
    hits = _recall.recall(query, k=k)
    if not hits:
        return "no relevant memories found"
    return "\n".join(hits)


def builtin_memory_tools() -> List[Tool]:
    return [
        Tool(
            name="remember",
            description=(
                "Save a durable fact about the user to long-term memory "
                "(preferences, decisions, commitments, names). Short factual "
                "lines only — never credentials; credential-like content is refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The fact to remember, e.g. 'Prabh prefers Hinglish'",
                    }
                },
                "required": ["fact"],
            },
            side_effect=SideEffect.WRITE,
            func=_remember,
        ),
        Tool(
            name="recall",
            description=(
                "Search long-term memory for facts relevant to a query. "
                "Use it when you need context about the user before answering."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search memory for",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Max snippets to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
            side_effect=SideEffect.READ,
            func=_recall_tool,
        ),
    ]
