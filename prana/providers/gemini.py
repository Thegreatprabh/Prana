"""Gemini provider (Generative Language API, generateContent).

API: POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
Tools are declared via `tools: [{functionDeclarations: [...]}]`; calls come
back as `functionCall` parts. Key travels as the `key` query parameter
(Google's documented convention for API keys) — never in logs or errors.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ..tools.base import ToolCall
from .base import Completion, Provider, ProviderError, redact


class GeminiProvider(Provider):
    name = "gemini"
    default_model = "gemini-2.5-flash"

    def _tools_payload(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        decls = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]
        return [{"functionDeclarations": decls}]

    def _url(self) -> str:
        qs = urllib.parse.urlencode({"key": self._api_key})
        return (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model, safe='')}:generateContent?{qs}"
        )

    def complete(self, messages, tools, **kwargs) -> Completion:
        system, convo = self._split_messages(messages)
        contents = []
        for m in convo:
            role = "model" if m.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
        body: Dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = self._tools_payload(tools)
        data = json.dumps(body).encode("utf-8")
        # NOTE: the URL contains the key — never log self._url().
        req = urllib.request.Request(
            self._url(), data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            raise ProviderError(
                f"gemini: request failed ({type(e).__name__}: {redact(str(e), self._api_key)})"
            ) from None

        if payload.get("error"):
            msg = payload["error"].get("message", "unknown error")
            raise ProviderError(f"gemini: API error: {redact(str(msg), self._api_key)}")

        text_parts: List[str] = []
        calls: List[ToolCall] = []
        try:
            parts = payload["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            raise ProviderError("gemini: malformed response (no candidates[0].content.parts)")
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            fc = part.get("functionCall")
            if fc:
                args = fc.get("args", {})
                if not isinstance(args, dict):
                    args = {}
                calls.append(ToolCall(id=fc.get("name", ""), name=fc.get("name", ""), arguments=args))
        return Completion(text="".join(text_parts), tool_calls=calls)
