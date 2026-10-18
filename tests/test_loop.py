"""Tests for the ReAct core loop with a scripted fake provider.

The fake provider implements the Provider interface without any network,
so these tests exercise the REAL loop + REAL tools end to end.
"""

import pytest

from prana import events
from prana.loop import AgentLoop
from prana.providers.base import Completion, Provider
from prana.tools import ToolRegistry, builtin_file_tools, builtin_shell_tools
from prana.tools.base import ToolCall


class ScriptedProvider(Provider):
    """Replays a script of Completions; records what it was given."""

    name = "fake"
    default_model = "fake-1"

    def __init__(self, script, **kw):
        super().__init__(api_key="fake-key", **kw)
        self.script = list(script)
        self.calls = 0
        self.last_messages = None
        self.last_tools = None

    def complete(self, messages, tools, **kwargs):
        self.calls += 1
        self.last_messages = messages
        self.last_tools = tools
        if not self.script:
            return Completion(text="script exhausted")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _registry(tmp_path):
    reg = ToolRegistry(approver=lambda n, a: True)
    for t in builtin_file_tools(str(tmp_path)):
        reg.register(t)
    for t in builtin_shell_tools(str(tmp_path)):
        reg.register(t)
    return reg


def test_loop_runs_tool_then_answers(tmp_path):
    (tmp_path / "note.txt").write_text("PRANA stands for life-breath")
    prov = ScriptedProvider([
        Completion(tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "note.txt"})]),
        Completion(text="The file says: PRANA stands for life-breath"),
    ])
    loop = AgentLoop(prov, _registry(tmp_path))
    res = loop.run(events.user_message("what does the note say?"))
    assert res.stop_reason == "done"
    assert "life-breath" in res.final_text
    assert res.tool_calls_made == 1
    assert res.iterations == 2
    # the provider saw the tool specs
    assert any(t["name"] == "read" for t in prov.last_tools)


def test_loop_stops_at_max_iters(tmp_path):
    prov = ScriptedProvider(
        [Completion(tool_calls=[ToolCall(id="c", name="list", arguments={})])] * 100
    )
    loop = AgentLoop(prov, _registry(tmp_path), max_iters=3)
    res = loop.run(events.user_message("list forever"))
    assert res.stop_reason == "max_iters"
    assert res.iterations == 3
    assert res.tool_calls_made == 3


def test_loop_truncates_huge_tool_output(tmp_path):
    (tmp_path / "big.txt").write_text("Z" * 50_000)
    prov = ScriptedProvider([
        Completion(tool_calls=[ToolCall(id="c1", name="read", arguments={"path": "big.txt"})]),
        Completion(text="done"),
    ])
    loop = AgentLoop(prov, _registry(tmp_path))
    res = loop.run(events.user_message("read big"))
    assert res.stop_reason == "done"
    # the observation fed back was truncated to ~4000 chars, not 50k
    assert len(res.observations[0]) < 6000
    assert "truncated" in res.observations[0]


def test_loop_handles_provider_error_gracefully(tmp_path):
    prov = ScriptedProvider([RuntimeError("boom sk-fake-key-123")])
    loop = AgentLoop(prov, _registry(tmp_path))
    res = loop.run(events.user_message("hi"))
    assert res.stop_reason == "provider_error"
    assert "boom" in res.final_text
    assert "sk-fake-key-123" not in res.final_text  # key never surfaces


def test_loop_handles_keyboard_interrupt(tmp_path):
    prov = ScriptedProvider([KeyboardInterrupt()])
    loop = AgentLoop(prov, _registry(tmp_path))
    res = loop.run(events.user_message("hi"))
    assert res.stop_reason == "interrupted"


def test_loop_unknown_tool_reported_not_crash(tmp_path):
    prov = ScriptedProvider([
        Completion(tool_calls=[ToolCall(id="c1", name="teleport", arguments={})]),
        Completion(text="that tool does not exist"),
    ])
    loop = AgentLoop(prov, _registry(tmp_path))
    res = loop.run(events.user_message("teleport me"))
    assert res.stop_reason == "done"
    assert "unknown tool" in res.observations[0]


def test_loop_wall_clock_stops(tmp_path):
    import time

    class SlowProvider(ScriptedProvider):
        def complete(self, messages, tools, **kwargs):
            time.sleep(0.05)
            return Completion(tool_calls=[ToolCall(id="c", name="list", arguments={})])

    prov = SlowProvider([Completion(text="x")])
    loop = AgentLoop(prov, _registry(tmp_path), wall_clock_s=1, max_iters=1000)
    res = loop.run(events.user_message("go"))
    assert res.stop_reason == "wall_clock"


def test_event_context_reaches_system_prompt(tmp_path):
    prov = ScriptedProvider([Completion(text="ok")])
    loop = AgentLoop(prov, _registry(tmp_path))
    loop.run(events.cron_tick("daily digest", job="digest"))
    system = prov.last_messages[0]["content"]
    assert "cron_tick" in system and "job=digest" in system
