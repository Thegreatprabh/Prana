"""Provider interface — one normalized shape for every LLM backend.

The core loop speaks only this interface. Each provider translates between
PRANA's internal format and its own API:

- messages: [{"role": "system"|"user"|"assistant", "content": str}]
- tools:    [{"name", "description", "parameters" (JSON schema)}]
- result:   Completion(text, tool_calls=[ToolCall, ...])

Secrets discipline: the API key is used ONLY in the Authorization header
(or provider-equivalent). It is never interpolated into logs, errors, URLs,
or message content. Error paths must call `redact()` before surfacing text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..tools.base import ToolCall


@dataclass
class Completion:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    # Provider-native stop info, for debugging (never contains the key).
    stop_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """Raised for transport/API failures. Message must never contain the key."""


def redact(text: str, key: Optional[str]) -> str:
    """Remove any occurrence of the API key from a string before surfacing it."""
    if not text or not key:
        return text
    return text.replace(key, "[REDACTED_KEY]")


class Provider(ABC):
    """A chat-completions-capable LLM backend with tool calling."""

    name: str = "base"
    default_model: str = ""

    def __init__(self, api_key: str, model: Optional[str] = None, timeout: int = 120) -> None:
        if not api_key or not api_key.strip():
            raise ProviderError(f"{self.name}: empty API key")
        self._api_key = api_key
        self.model = model or self.default_model
        self.timeout = timeout

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, str]],
        tools: List[Dict[str, Any]],
        **kwargs: Any,
    ) -> Completion:
        """One chat turn. Returns text and/or tool calls."""
        raise NotImplementedError

    # -- helpers shared by implementations ---------------------------------
    def _split_messages(self, messages: List[Dict[str, str]]):
        """Split system prompt from the conversation (most APIs want it separate)."""
        system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
        convo = [m for m in messages if m.get("role") != "system"]
        return "\n\n".join(system_parts), convo
