#!/usr/bin/env python3
"""PRANA scripted demo — the whole system, zero keys, zero network.

Exercises: chat turn -> remember -> recall across a simulated restart ->
schedule a job -> daemon tick -> delivery decision -> Telegram update -> reply.

Exit 0 = everything works. Any failure prints FAIL and exits 1.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# --- isolate ALL state (config, memory, jobs) into a temp HOME ---------------
_tmp = tempfile.mkdtemp(prefix="prana-demo-")
os.environ["HOME"] = _tmp
os.environ.pop("PRANA_MEMORY_DIR", None)

from prana import events  # noqa: E402
from prana.channels import telegram  # noqa: E402
from prana.channels.run import handle_update, ConversationStore  # noqa: E402
from prana.loop import AgentLoop  # noqa: E402
from prana.memory import store as mem_store  # noqa: E402
from prana.proactivity import daemon, delivery, hooks as hookmod, jobs, workers  # noqa: E402
from prana.providers.base import Completion, Provider  # noqa: E402
from prana.tools import ToolRegistry, builtin_tools  # noqa: E402
from prana.tools.base import ToolCall  # noqa: E402
from prana.tools.memory_tools import builtin_memory_tools  # noqa: E402
from prana.tools.schedule_tool import _do_schedule  # noqa: E402

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class Scripted(Provider):
    """Fake provider: plays a script of Completions, no network."""

    name = "scripted"
    default_model = "scripted-1"

    def __init__(self, script):
        super().__init__(api_key="demo-not-a-key")
        self.script = list(script)
        self.seen_system = []

    def complete(self, messages, tools, **kw):
        self.seen_system.append(messages[0]["content"] if messages else "")
        if self.script:
            return self.script.pop(0)
        return Completion(text="FINAL: done", tool_calls=[])


def make_loop(script) -> tuple[AgentLoop, Scripted]:
    prov = Scripted(script)
    reg = ToolRegistry()
    for t in builtin_tools(sandbox_root=_tmp):
        reg.register(t)
    for t in builtin_memory_tools():
        reg.register(t)
    return AgentLoop(provider=prov, registry=reg), prov


def main() -> int:
    # 1. chat turn ---------------------------------------------------------
    loop, _ = make_loop([Completion(text="FINAL: hello from PRANA", tool_calls=[])])
    r = loop.run(events.user_message("hello"))
    check("chat turn completes", "hello from PRANA" in r.final_text, r.final_text[:60])

    # 2. remember via a real tool call -------------------------------------
    loop2, _ = make_loop([
        Completion(text="noted", tool_calls=[
            ToolCall(id="c1", name="remember",
                     arguments={"fact": "Prabh's demo fruit is mango"})]),
        Completion(text="FINAL: remembered", tool_calls=[]),
    ])
    r2 = loop2.run(events.user_message("remember my demo fruit is mango"))
    check("remember tool executed", "mango" in mem_store.read_memory(),
          "fact persisted to MEMORY.md")

    # 3. recall across a simulated restart ----------------------------------
    loop3, prov3 = make_loop([Completion(text="FINAL: mango", tool_calls=[])])
    loop3.run(events.user_message("what is my demo fruit?"))
    sys_prompt = prov3.seen_system[-1] if prov3.seen_system else ""
    check("recall injects memory after restart", "mango" in sys_prompt,
          "fresh loop saw the remembered fact")

    # 4. schedule a job via the schedule tool -------------------------------
    out = _do_schedule(name="demo-ping", schedule="every 1m",
                       request="Say hi to Prabh", delivery_target="democap:x")
    created = jobs.list_jobs()
    check("schedule tool creates job", len(created) == 1 and "demo-ping" in out,
          created[0].id if created else "none")

    # 5. daemon tick -> loop runs -> delivery decision -----------------------
    delivered: list = []
    delivery.register_sink("democap", lambda dest, text: delivered.append((dest, text)))
    tick_loop, _ = make_loop([Completion(text="FINAL: hi Prabh!", tool_calls=[])])
    pool = workers.WorkerPool(lambda: make_loop(
        [Completion(text="FINAL: hi Prabh!", tool_calls=[])])[0])
    watcher = hookmod.HookWatcher()
    summary = daemon.tick_once(tick_loop, pool, watcher, datetime.now(timezone.utc))
    check("daemon tick ran the due job", summary.get("jobs_due") == 1, str(summary))
    check("delivery fired on first run",
          len(delivered) == 1 and "hi Prabh" in delivered[0][1],
          str(delivered)[:80])

    # 5b. second tick: same result would be silent (no double-ping) ---------
    delivered.clear()
    tick_loop2, _ = make_loop([Completion(text="FINAL: hi Prabh!", tool_calls=[])])
    summary2 = daemon.tick_once(tick_loop2, pool, watcher, datetime.now(timezone.utc))
    check("no duplicate delivery on re-tick", summary2.get("jobs_due", 1) == 0,
          str(summary2))
    jobs.remove_job(created[0].id)

    # 6. Telegram update -> reply (mocked transport) -------------------------
    bot = telegram.TelegramBot(token="demo-token")
    tg_loop, _ = make_loop([Completion(text="FINAL: telegram works", tool_calls=[])])
    tstore = ConversationStore()
    update = {"update_id": 7,
              "message": {"chat": {"id": 99}, "text": "ping"}}
    sent: list = []

    def fake_post(method, payload):
        if method == "sendMessage":
            sent.append(payload)
            return {"message_id": 1}
        return {}

    with patch.object(telegram.TelegramBot, "_post", side_effect=fake_post):
        ok = handle_update(bot, tg_loop, tstore, update)
    check("telegram update -> agent reply",
          ok and len(sent) == 2
          and sent[0]["text"].startswith("🔗 Paired!")  # pairing notice first
          and "telegram works" in sent[1]["text"]
          and sent[1]["chat_id"] == "99",
          str(sent)[:80])

    # done ------------------------------------------------------------------
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("PRANA demo: all systems green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
