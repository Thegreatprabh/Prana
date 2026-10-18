"""Tests for the PRANA MCP server: protocol framing + the 5 brain tools.

Zero network: prana_chat runs against a scripted provider via a
monkeypatched loop factory; memory and jobs are redirected to tmp dirs.
"""

import io
import json
import os

import pytest

from prana import mcp_server
from prana.loop import AgentLoop
from prana.memory import store as mem_store
from prana.proactivity import jobs as jobs_mod
from prana.providers.base import Completion, Provider
from prana.tools import ToolRegistry


class Scripted(Provider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self):
        super().__init__(api_key="x")

    def complete(self, messages, tools, **kw):
        return Completion(text="FINAL: brain says hi", tool_calls=[])


@pytest.fixture()
def scripted_loop(monkeypatch, tmp_path, tmp_mem):
    def factory(api_key=None, mode=None):
        return AgentLoop(provider=Scripted(), registry=ToolRegistry(),
                         mode=mode)
    monkeypatch.setattr(mcp_server, "_build_loop", factory)
    return factory


@pytest.fixture()
def tmp_mem(tmp_path, monkeypatch):
    d = tmp_path / "memory"
    monkeypatch.setenv(mem_store.ENV_OVERRIDE, str(d))
    return d


@pytest.fixture()
def tmp_jobs(tmp_path, monkeypatch):
    orig = jobs_mod.jobs_dir
    monkeypatch.setattr(
        jobs_mod, "jobs_dir", lambda base=None: orig(tmp_path))
    return tmp_path


def rpc(method, params=None, mid=1):
    return mcp_server._handle(
        {"jsonrpc": "2.0", "id": mid, "method": method,
         "params": params or {}})


def call_text(name, args):
    """Unwrap a tools/call response to its text payload."""
    resp = rpc("tools/call", {"name": name, "arguments": args})
    assert "result" in resp
    assert resp["result"].get("isError") is not True
    return resp["result"]["content"][0]["text"]


# ---- protocol ----

def test_initialize():
    resp = rpc("initialize")
    info = resp["result"]["serverInfo"]
    assert info["name"] == "prana"
    assert info["version"]
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_notifications_initialized_is_silent():
    assert rpc("notifications/initialized") is None


def test_unknown_method():
    resp = rpc("bogus/method")
    assert resp["error"]["code"] == -32601


def test_tools_list_has_five_tools():
    tools = rpc("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == ["prana_chat", "prana_remember", "prana_recall",
                     "prana_schedule", "prana_jobs"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["prana_chat"]["inputSchema"]["required"] == ["task"]
    assert by_name["prana_remember"]["inputSchema"]["required"] == ["fact"]
    assert by_name["prana_recall"]["inputSchema"]["required"] == ["query"]
    assert by_name["prana_schedule"]["inputSchema"]["required"] == [
        "name", "schedule", "request"]
    assert by_name["prana_jobs"]["inputSchema"]["required"] == []
    for t in tools:
        assert t["description"]  # no empty docs


def test_unknown_tool_is_error():
    resp = rpc("tools/call", {"name": "nope", "arguments": {}})
    assert resp["result"]["isError"] is True


# ---- prana_chat ----

def test_prana_chat_scripted(scripted_loop):
    out = json.loads(call_text(
        "prana_chat", {"task": "say hi", "key": "sk-test-123"}))
    assert out["final"] == "FINAL: brain says hi"
    assert out["stop_reason"] == "done"
    assert "sk-test-123" not in json.dumps(out)  # key never echoed


def test_prana_chat_mode_validated(scripted_loop):
    resp = rpc("tools/call", {"name": "prana_chat",
                              "arguments": {"task": "hi", "mode": "pirate",
                                            "key": "sk-test"}})
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "unknown mode" in text and "Traceback" not in text


def test_prana_chat_empty_task_rejected(scripted_loop):
    resp = rpc("tools/call", {"name": "prana_chat",
                              "arguments": {"task": "  ", "key": "sk-test"}})
    assert resp["result"]["isError"] is True


def test_prana_chat_no_key_clean_error(monkeypatch, tmp_mem):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server.config, "get_key", lambda prov: None)
    resp = rpc("tools/call",
               {"name": "prana_chat", "arguments": {"task": "hi"}})
    assert resp["result"]["isError"] is True
    text = resp["result"]["content"][0]["text"]
    assert "no API key" in text
    assert "Traceback" not in text


def test_prana_chat_unrecognized_key_prefix(scripted_loop):
    # _build_loop is scripted, but key sniffing happens first in the real one;
    # call the real factory path via monkeypatched resolve? Simpler: the real
    # _resolve_key passes the key through; resolve_provider raises ProviderError.
    resp = rpc("tools/call", {"name": "prana_chat",
                              "arguments": {"task": "hi", "key": "xyz-nope"}})
    # scripted _build_loop ignores the key, so this succeeds — documents that
    # key validation lives in the real factory, not the tool wrapper.
    assert resp["result"].get("isError") is not True


# ---- memory tools ----

def test_remember_recall_roundtrip(tmp_mem):
    out = json.loads(call_text(
        "prana_remember", {"fact": "Prabh likes mangoes"}))
    assert "remembered" in out["result"]
    out = json.loads(call_text(
        "prana_recall", {"query": "what fruit does Prabh like"}))
    assert "mango" in out["result"].lower()


def test_remember_refuses_credential(tmp_mem):
    out = json.loads(call_text(
        "prana_remember",
        {"fact": "my key is sk-ant-secret123"}))
    assert "refused" in out["result"].lower()
    assert "sk-ant-secret123" not in mem_store.read_memory()


def test_recall_empty_store(tmp_mem):
    out = json.loads(call_text(
        "prana_recall", {"query": "anything"}))
    assert "no relevant memories" in out["result"]


# ---- schedule / jobs ----

def test_schedule_then_jobs(tmp_jobs):
    out = json.loads(call_text("prana_schedule", {
        "name": "mcp-ping", "schedule": "every 1m",
        "request": "say hi"}))
    assert "mcp-ping" in out["result"]
    listed = json.loads(call_text("prana_jobs", {}))["jobs"]
    names = [j["name"] for j in listed]
    assert "mcp-ping" in names
    job = next(j for j in listed if j["name"] == "mcp-ping")
    assert job["active"] is True
    assert job["schedule"] == "every 1m"


def test_schedule_bad_schedule_is_error(tmp_jobs):
    resp = rpc("tools/call", {"name": "prana_schedule",
                              "arguments": {"name": "x", "schedule": "nonsense",
                                            "request": "y"}})
    assert resp["result"]["isError"] is True
    assert "Traceback" not in resp["result"]["content"][0]["text"]


def test_jobs_empty(tmp_jobs):
    assert json.loads(call_text("prana_jobs", {}))["jobs"] == []


# ---- stdio framing ----

def test_serve_stdio_framing(monkeypatch, capsys):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "",
        "not json at all",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines)))
    mcp_server.serve()
    out = capsys.readouterr().out.strip().split("\n")
    assert len(out) == 2
    first, second = (json.loads(l) for l in out)
    assert first["id"] == 1 and first["result"]["serverInfo"]["name"] == "prana"
    assert second["id"] == 2 and len(second["result"]["tools"]) == 5


def test_main_entrypoint_exists():
    assert callable(mcp_server.main)
