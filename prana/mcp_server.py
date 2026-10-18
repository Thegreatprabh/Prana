"""PRANA MCP server: exposes the PRANA brain over JSON-RPC stdio.

Speaks the MCP stdio transport (JSON-RPC 2.0, newline-delimited):
  initialize -> { protocolVersion, serverInfo }
  tools/list -> the 5 PRANA brain tools + schemas
  tools/call -> run a tool, get results

This is what plugs PRANA into Claude Code, Claude Desktop, Cursor and
ChatGPT Desktop: any MCP-capable agent gets PRANA's brain — a real
ReAct loop with tools, persistent memory, proactivity and personality.

Wire it into any MCP client:

    { "mcpServers": { "prana": {
        "command": "python",
        "args": ["-m", "prana.mcp_server"] } } }

(On Windows use the full python path in "command", same as any MCP server.
Once `prana-mcp` is wired as a console script, `"command": "prana-mcp"
works with no args.)

API key for prana_chat — tool arg `key` > env (OPENAI_API_KEY /
ANTHROPIC_API_KEY / GEMINI_API_KEY) > ~/.prana/config.json.
The provider is auto-detected from the key prefix (sk-ant- / AIza / sk-).
The key is never logged, never echoed, never returned.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional

from . import __version__, config, events
from .loop import AgentLoop
from .proactivity import jobs as _jobs
from .providers.base import ProviderError
from .providers.detect import resolve_provider
from .tools import ToolRegistry, builtin_tools
from .tools.memory_tools import _recall_tool, _remember, builtin_memory_tools
from .tools.schedule_tool import _do_schedule

ToolFn = Callable[..., Dict[str, Any]]
_REGISTRY: Dict[str, Dict[str, Any]] = {}


def tool(name: str, description: str,
         arg_docs: Optional[Dict[str, str]] = None):
    """Register an MCP tool. Schema is derived from the function signature;
    every parameter is a string; parameters without defaults are required."""
    def deco(fn: ToolFn) -> ToolFn:
        sig = inspect.signature(fn)
        docs = arg_docs or {}
        props: Dict[str, Any] = {}
        required: List[str] = []
        for pname, param in sig.parameters.items():
            props[pname] = {"type": "string",
                            "description": docs.get(pname, f"argument `{pname}`")}
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        _REGISTRY[name] = {
            "fn": fn,
            "description": description,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
        }
        return fn
    return deco


_MODES = ("casual", "teacher", "engineer", "strategist")


def _resolve_key(explicit: Optional[str]) -> str:
    """API key precedence: tool arg > env > ~/.prana/config.json.

    Mirrors prana.__main__.resolve_key. Raises ProviderError (clean message,
    never the key) when nothing is configured.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    for prov in ("openai", "anthropic", "gemini"):
        key = config.get_key(prov)
        if key:
            return key
    raise ProviderError(
        "no API key found — pass the `key` argument, set OPENAI_API_KEY / "
        "ANTHROPIC_API_KEY / GEMINI_API_KEY, or run `prana --save-key`"
    )


def _build_loop(api_key: str, mode: Optional[str] = None) -> AgentLoop:
    """A fresh AgentLoop: auto-detected provider, all built-in tools, memory on.

    Module-level so tests can monkeypatch it with a scripted provider.
    File/shell tools are sandboxed to the server's working directory.
    """
    provider_cls = resolve_provider(api_key)
    provider = provider_cls(api_key=api_key)
    registry = ToolRegistry()
    for t in builtin_tools(sandbox_root=os.getcwd()):
        registry.register(t)
    for t in builtin_memory_tools():
        registry.register(t)
    return AgentLoop(provider, registry, mode=mode)


@tool(
    "prana_chat",
    "Run one task through PRANA's full brain: ReAct reasoning loop, shell/files/web "
    "tools, persistent memory, and a personality mode. Returns the final answer.",
    {"task": "the task or question for the agent",
     "mode": "personality: casual | teacher | engineer | strategist (default: from identity)",
     "key": "LLM API key (else env OPENAI_API_KEY/ANTHROPIC_API_KEY/GEMINI_API_KEY or ~/.prana/config.json)"},
)
def t_prana_chat(task: str, mode: str = "", key: str = "") -> Dict[str, Any]:
    if not task or not task.strip():
        raise ValueError("`task` must not be empty")
    chosen_mode = (mode or "").strip().lower() or None
    if chosen_mode and chosen_mode not in _MODES:
        raise ValueError(
            f"unknown mode '{mode}' — choose from: {', '.join(_MODES)}")
    api_key = _resolve_key(key or None)
    loop = _build_loop(api_key, chosen_mode)
    result = loop.run(events.user_message(task.strip()))
    return {
        "final": result.final_text,
        "iterations": result.iterations,
        "tool_calls": result.tool_calls_made,
        "stop_reason": result.stop_reason,
        "elapsed_s": round(result.elapsed_s, 1),
    }


@tool(
    "prana_remember",
    "Store a durable fact in PRANA's long-term memory. Survives restarts. "
    "Credential-like values are refused, never stored.",
    {"fact": "one durable fact, e.g. 'Prabh prefers Hinglish'"},
)
def t_prana_remember(fact: str) -> Dict[str, Any]:
    if not fact or not fact.strip():
        raise ValueError("`fact` must not be empty")
    return {"result": _remember(fact.strip())}


@tool(
    "prana_recall",
    "Search PRANA's long-term memory for relevant snippets.",
    {"query": "what to search memory for"},
)
def t_prana_recall(query: str) -> Dict[str, Any]:
    if not query or not query.strip():
        raise ValueError("`query` must not be empty")
    return {"result": _recall_tool(query.strip())}


@tool(
    "prana_schedule",
    "Create a persistent scheduled job: the PRANA daemon runs `request` on "
    "`schedule` and delivers results. Schedules: cron '30 9 * * *', "
    "@hourly/@daily/@weekly, or 'every 30m' / 'every 2h' / 'every 1d'.",
    {"name": "short human name, e.g. 'morning digest'",
     "schedule": "when to run: cron, @daily, or 'every 30m'",
     "request": "the task the agent works on at every tick",
     "delivery_target": "'console' (default) or 'telegram:CHAT_ID'"},
)
def t_prana_schedule(name: str, schedule: str, request: str,
                     delivery_target: str = "console") -> Dict[str, Any]:
    return {"result": _do_schedule(
        name.strip(), schedule.strip(), request.strip(),
        (delivery_target or "console").strip())}


@tool(
    "prana_jobs",
    "List PRANA's scheduled jobs: name, schedule, active state, delivery target.",
    {},
)
def t_prana_jobs() -> Dict[str, Any]:
    return {"jobs": [
        {"id": j.id, "name": j.name, "schedule": j.schedule,
         "request": j.request, "active": j.active,
         "delivery_target": j.delivery_target,
         "last_run": j.last_run, "created_at": j.created_at}
        for j in _jobs.list_jobs()
    ]}


def _handle(msg: Dict[str, Any]) -> Dict[str, Any] | None:
    # Malformed input must never kill the server: validate shapes first.
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request: not an object"}}
    method = msg.get("method", "")
    mid = msg.get("id")
    params = msg.get("params", {}) or {}
    if not isinstance(params, dict):
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32600, "message": "invalid request: params not an object"}}

    def ok(result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05",
                   "serverInfo": {"name": "prana", "version": __version__},
                   "capabilities": {"tools": {}}})
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return ok({"tools": [
            {"name": n, "description": t["description"],
             "inputSchema": t["inputSchema"]}
            for n, t in _REGISTRY.items()]})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        entry = _REGISTRY.get(name)
        if not entry:
            return ok({"content": [{"type": "text",
                                    "text": f"unknown tool: {name}"}],
                       "isError": True})
        try:
            out = entry["fn"](**args)
            text = json.dumps(out, indent=2)[:20000]
        except Exception as exc:  # never crash the agent loop, never leak keys
            text = json.dumps({"error": str(exc)})
            return ok({"content": [{"type": "text", "text": text}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": text}]})
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"unknown method {method}"}}


def serve() -> None:
    """Newline-delimited JSON-RPC on stdio (the MCP stdio transport)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def main(argv=None) -> None:
    """Entry point: ``prana-mcp``. ``--help``/``--version`` handled, else serve stdio."""
    import argparse

    p = argparse.ArgumentParser(
        prog="prana-mcp",
        description="PRANA MCP server — exposes PRANA's brain (chat, memory, "
        "scheduling) to Claude Code, Claude Desktop, Cursor, ChatGPT Desktop "
        "over stdio JSON-RPC.",
    )
    p.add_argument("--version", action="version", version="prana-mcp 0.1.0")
    p.parse_args(argv)
    serve()


if __name__ == "__main__":
    main()
