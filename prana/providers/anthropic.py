"""Anthropic provider (Messages API).

API: POST https://api.anthropic.com/v1/messages with `tools` (input_schema).
Key goes in `x-api-key` + `anthropic-version` headers only.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List

from ..tools.base import ToolCall
from .base import Completion, Provider, ProviderError, redact

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"
    default_model = "claude-sonnet-4-5"

    def _tools_payload(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        }

    def complete(self, messages, tools, **kwargs) -> Completion:
        system, convo = self._split_messages(messages)
        body: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(kwargs.get("max_tokens", 4096)),
            "messages": [{"role": m["role"], "content": m["content"]} for m in convo],
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = self._tools_payload(tools)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(API_URL, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise ProviderError(
                f"anthropic: request failed ({type(e).__name__}: {redact(str(e), self._api_key)})"
            ) from None

        if payload.get("type") == "error":
            msg = payload.get("error", {}).get("message", "unknown error")
            raise ProviderError(f"anthropic: API error: {redact(str(msg), self._api_key)}")

        text_parts: List[str] = []
        calls: List[ToolCall] = []
        for block in payload.get("content", []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                args = block.get("input", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append(ToolCall(id=str(block.get("id", "")), name=block.get("name", ""), arguments=args))
        return Completion(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=str(payload.get("stop_reason", "")),
            usage=payload.get("usage", {}),
        )
