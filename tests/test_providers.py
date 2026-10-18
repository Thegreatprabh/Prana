"""Tests for providers: detection, redaction, payload translation, response parsing.

No network is touched. The OpenAI response parser is tested by monkeypatching
urllib.request.urlopen with a canned HTTP response.
"""

import io
import json

import pytest

from prana.providers import (
    AnthropicProvider,
    GeminiProvider,
    OpenAIProvider,
    ProviderError,
    detect_provider,
    redact,
    resolve_provider,
)
from prana.tools.base import ToolCall


# ---- detection ----

def test_detect_anthropic():
    assert detect_provider("sk-ant-abc123") == "anthropic"


def test_detect_gemini():
    assert detect_provider("AIzaSyD-abc123") == "gemini"


def test_detect_openai():
    assert detect_provider("sk-proj-abc123") == "openai"


def test_detect_unknown_prefix_errors():
    with pytest.raises(ProviderError, match="could not detect provider"):
        detect_provider("xx-12345")


def test_detect_empty_key_errors():
    with pytest.raises(ProviderError, match="no API key"):
        detect_provider("")


def test_detect_never_echoes_key():
    try:
        detect_provider("xx-SUPERSECRETKEY")
    except ProviderError as e:
        assert "SUPERSECRETKEY" not in str(e)


def test_resolve_override_wins():
    assert resolve_provider("sk-ant-abc", provider="openai") is OpenAIProvider


def test_resolve_unknown_provider_errors():
    with pytest.raises(ProviderError, match="unknown provider"):
        resolve_provider("sk-abc", provider="watson")


def test_resolve_sniffs_when_no_override():
    assert resolve_provider("AIza123") is GeminiProvider


# ---- redaction ----

def test_redact_removes_key():
    assert redact("boom sk-ant-SECRET tail", "sk-ant-SECRET") == "boom [REDACTED_KEY] tail"


def test_redact_no_key_is_noop():
    assert redact("hello", None) == "hello"


# ---- payload translation ----

def _spec():
    return [{
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }]


def test_openai_tool_payload_shape():
    p = OpenAIProvider(api_key="sk-test")
    payload = p._tools_payload(_spec())
    fn = payload[0]
    assert fn["type"] == "function"
    assert fn["function"]["name"] == "read"
    assert "path" in fn["function"]["parameters"]["properties"]


def test_anthropic_tool_payload_shape():
    p = AnthropicProvider(api_key="sk-ant-test")
    payload = p._tools_payload(_spec())
    assert payload[0]["name"] == "read"
    assert "path" in payload[0]["input_schema"]["properties"]


def test_gemini_tool_payload_shape():
    p = GeminiProvider(api_key="AIza-test")
    payload = p._tools_payload(_spec())
    decls = payload[0]["functionDeclarations"]
    assert decls[0]["name"] == "read"


def test_gemini_url_contains_model_not_logged_elsewhere():
    p = GeminiProvider(api_key="AIza-test", model="gemini-x")
    assert "gemini-x" in p._url()
    # the key must be redactable out of anything surfaced
    assert redact(p._url(), "AIza-test") == p._url().replace("AIza-test", "[REDACTED_KEY]")


# ---- response parsing (canned HTTP, no network) ----

class _FakeHTTP:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _patch_urlopen(monkeypatch, payload: dict):
    data = json.dumps(payload).encode()

    def fake(req, timeout=None):
        return _FakeHTTP(data)

    monkeypatch.setattr("urllib.request.urlopen", fake)


def test_openai_parses_tool_calls(monkeypatch):
    _patch_urlopen(monkeypatch, {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
                }],
            },
        }],
        "usage": {},
    })
    p = OpenAIProvider(api_key="sk-test")
    out = p.complete([{"role": "user", "content": "hi"}], _spec())
    assert len(out.tool_calls) == 1
    call = out.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == "read" and call.arguments == {"path": "a.txt"}


def test_openai_api_error_redacts_key(monkeypatch):
    _patch_urlopen(monkeypatch, {"error": {"message": "bad key sk-test"}})
    p = OpenAIProvider(api_key="sk-test")
    with pytest.raises(ProviderError) as ei:
        p.complete([{"role": "user", "content": "hi"}], [])
    assert "sk-test" not in str(ei.value)
    assert "[REDACTED_KEY]" in str(ei.value)


def test_anthropic_parses_tool_use(monkeypatch):
    _patch_urlopen(monkeypatch, {
        "type": "message",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "reading now"},
            {"type": "tool_use", "id": "tu_1", "name": "read", "input": {"path": "b.txt"}},
        ],
        "usage": {},
    })
    p = AnthropicProvider(api_key="sk-ant-test")
    out = p.complete([{"role": "user", "content": "hi"}], _spec())
    assert out.text == "reading now"
    assert out.tool_calls[0].name == "read"
    assert out.tool_calls[0].arguments == {"path": "b.txt"}


def test_gemini_parses_function_call(monkeypatch):
    _patch_urlopen(monkeypatch, {
        "candidates": [{
            "content": {"parts": [
                {"text": "ok"},
                {"functionCall": {"name": "read", "args": {"path": "c.txt"}}},
            ]},
        }],
    })
    p = GeminiProvider(api_key="AIza-test")
    out = p.complete([{"role": "user", "content": "hi"}], _spec())
    assert out.text == "ok"
    assert out.tool_calls[0].name == "read"
    assert out.tool_calls[0].arguments == {"path": "c.txt"}


def test_empty_key_rejected():
    with pytest.raises(ProviderError):
        OpenAIProvider(api_key="  ")
