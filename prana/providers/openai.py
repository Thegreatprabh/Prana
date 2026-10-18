"""OpenAI provider (also serves any OpenAI-compatible endpoint).

API: POST {base}/chat/completions with `tools` (function calling).
Key goes in `Authorization: Bearer` only.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List

from ..tools.base import ToolCall
from .base import Completion, Provider, ProviderError, redact


class OpenAIProvider(Provider):
    name = "openai"
    default_model = "gpt-4o-mini"

    def __init__(self, api_key: str, model: str | None = None, timeout: int = 120,
                 base_url: str = "https://api.openai.com/v1") -> None:
        super().__init__(api_key, model, timeout)
        self.base_url = base_url.rstrip("/")

    def _tools_payload(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def complete(self, messages, tools, **kwargs) -> Completion:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        }
        if tools:
            body["tools"] = self._tools_payload(tools)
            body["tool_choice"] = "auto"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=data, headers=self._headers()
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise ProviderError(
                f"openai: request failed ({type(e).__name__}: {redact(str(e), self._api_key)})"
            ) from None

        if payload.get("error"):
            msg = payload["error"].get("message", "unknown error")
            raise ProviderError(f"openai: API error: {redact(str(msg), self._api_key)}")

        try:
            choice = payload["choices"][0]["message"]
        except (KeyError, IndexError):
            raise ProviderError("openai: malformed response (no choices[0].message)")

        text = choice.get("content") or ""
        calls: List[ToolCall] = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=str(tc.get("id", "")), name=fn.get("name", ""), arguments=args))
        return Completion(
            text=text,
            tool_calls=calls,
            stop_reason=str(payload["choices"][0].get("finish_reason", "")),
            usage=payload.get("usage", {}),
        )
