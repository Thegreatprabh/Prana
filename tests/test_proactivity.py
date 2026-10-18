"""Tests for prana.proactivity — scheduler, jobs, hooks, workers, delivery.

All tests are hermetic: tmp dirs for storage, fake clocks, scripted
providers, short poll intervals. No network, no real API keys.
"""

from datetime import datetime, timedelta, timezone

import pytest

from prana import events
from prana.loop import RunResult
from prana.proactivity import delivery, hooks, jobs, scheduler, workers
from prana.proactivity.delivery import DeliveryError
from prana.proactivity.hooks import HookWatcher, add_hook, list_hooks, remove_hook
from prana.proactivity.jobs import (
    create_job,
    list_jobs,
    load_job,
    remove_job,
    set_active,
)
from prana.tools import ToolCall, ToolRegistry, builtin_schedule_tools


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# scheduler: parsing
# ---------------------------------------------------------------------------

def test_parse_star_cron():
    s = scheduler.parse_schedule("* * * * *")
    assert s.kind == "cron" and len(s.cron.minute) == 60


def test_parse_step_and_list():
    s = scheduler.parse_schedule("*/15 9-17 * * 1,3,5")
    assert s.cron.minute == frozenset({0, 15, 30, 45})
    assert s.cron.hour == frozenset(range(9, 18))
    assert s.cron.dow == frozenset({1, 3, 5})


def test_parse_shorthands():
    assert scheduler.parse_schedule("@hourly").raw == "@hourly"
    assert scheduler.parse_schedule("@daily").cron.hour == frozenset({0})
    assert scheduler.parse_schedule("@weekly").cron.dow == frozenset({0})
    # Sunday alias: 7 == 0
    assert scheduler.parse_schedule("0 0 * * 7").cron.dow == frozenset({0})


def test_parse_every():
    assert scheduler.parse_schedule("every 30m").interval_s == 1800
    assert scheduler.parse_schedule("every 2h").interval_s == 7200
    assert scheduler.parse_schedule("every 1d").interval_s == 86400
    assert scheduler.parse_schedule("EVERY 10S").interval_s == 10


def test_parse_invalid():
    for bad in ["", "   ", "* * * *", "* * * * * *", "every 0m",
                "every xm", "61 * * * *", "0 25 * * *", "nope"]:
        with pytest.raises(ValueError):
            scheduler.parse_schedule(bad)


# ---------------------------------------------------------------------------
# scheduler: matching / due
# ---------------------------------------------------------------------------

def test_matches_cron():
    # 2026-10-18 is a Sunday.
    sunday_9 = utc(2026, 10, 18, 9, 0)
    cron = scheduler.parse_schedule("0 9 * * 0").cron
    assert scheduler.matches_cron(cron, sunday_9)
    assert not scheduler.matches_cron(cron, utc(2026, 10, 18, 9, 1))
    assert not scheduler.matches_cron(cron, utc(2026, 10, 19, 9, 0))  # Monday


def test_is_due_cron_no_double_fire():
    sched = scheduler.parse_schedule("0 9 * * *")
    nine = utc(2026, 10, 18, 9, 0, 20)
    assert scheduler.is_due(sched, nine, None)  # never run → due
    # Ran 10s ago inside the same matching minute → not due.
    assert not scheduler.is_due(sched, nine, utc(2026, 10, 18, 9, 0, 10))
    # Ran an hour ago → due again.
    assert scheduler.is_due(sched, nine, utc(2026, 10, 18, 8, 0, 0))
    # Not a matching minute → not due regardless.
    assert not scheduler.is_due(sched, utc(2026, 10, 18, 9, 30), None)


def test_is_due_interval():
    sched = scheduler.parse_schedule("every 1m")
    now = utc(2026, 10, 18, 12, 0, 0)
    assert scheduler.is_due(sched, now, None)
    assert scheduler.is_due(sched, now, now - timedelta(minutes=2))
    assert not scheduler.is_due(sched, now, now - timedelta(seconds=30))


def test_next_due_cron():
    sched = scheduler.parse_schedule("0 9 * * *")
    nxt = scheduler.next_due(sched, utc(2026, 10, 18, 8, 0))
    assert nxt == utc(2026, 10, 18, 9, 0)
    nxt2 = scheduler.next_due(sched, utc(2026, 10, 18, 9, 0))
    assert nxt2 == utc(2026, 10, 19, 9, 0)  # strictly after


def test_next_due_interval():
    sched = scheduler.parse_schedule("every 1h")
    base = utc(2026, 10, 18, 8, 15)
    assert scheduler.next_due(sched, base, last_run=base) == utc(2026, 10, 18, 9, 15)


def test_due_jobs_skips_inactive_and_bad(tmp_path):
    from prana.proactivity import jobs as jm
    j1 = jm.create_job("a", "every 1m", "do a", base=tmp_path)
    j2 = jm.create_job("b", "every 1m", "do b", base=tmp_path)
    jm.set_active(j2.id, False, base=tmp_path)
    j3 = jm.create_job("c", "not a schedule", "do c", base=tmp_path)
    now = utc(2026, 10, 18, 12, 0)
    due = scheduler.due_jobs(jm.list_jobs(base=tmp_path), now)
    assert [j.id for j in due] == [j1.id]  # inactive + corrupt skipped
    assert j3.id not in [j.id for j in due]


# ---------------------------------------------------------------------------
# jobs: CRUD + persistence
# ---------------------------------------------------------------------------

def test_job_crud_roundtrip(tmp_path):
    job = create_job("digest", "@daily", "summarize news",
                     delivery_target="console", base=tmp_path)
    assert job.id and job.active
    loaded = load_job(job.id, base=tmp_path)
    assert loaded is not None and loaded.name == "digest"
    assert loaded.schedule == "@daily" and loaded.request == "summarize news"

    assert load_job("nope", base=tmp_path) is None

    names = [j.name for j in list_jobs(base=tmp_path)]
    assert names == ["digest"]

    assert set_active(job.id, False, base=tmp_path).active is False
    assert remove_job(job.id, base=tmp_path) is True
    assert remove_job(job.id, base=tmp_path) is False
    assert list_jobs(base=tmp_path) == []


def test_job_validation(tmp_path):
    for kwargs in [dict(name="", schedule="@daily", request="x"),
                   dict(name="n", schedule="  ", request="x"),
                   dict(name="n", schedule="@daily", request="")]:
        with pytest.raises(ValueError):
            create_job(base=tmp_path, **kwargs)


def test_job_file_perms_0600(tmp_path):
    import os, stat
    job = create_job("secret-task", "@daily", "do the thing", base=tmp_path)
    path = tmp_path / "jobs" / f"{job.id}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# delivery: the decision
# ---------------------------------------------------------------------------

class _J:
    def __init__(self, h=None):
        self.last_result_hash = h


def test_delivery_empty_never():
    j = _J(None)
    assert delivery.should_deliver(j, "") is False
    assert delivery.should_deliver(j, "   \n  ") is False
    assert delivery.should_deliver(j, None) is False


def test_delivery_first_run_delivers():
    assert delivery.should_deliver(_J(None), "hello world") is True


def test_delivery_unchanged_silent_changed_delivers():
    h = delivery.result_hash("same text")
    assert delivery.should_deliver(_J(h), "same text") is False
    assert delivery.should_deliver(_J(h), "different text") is True


def test_delivery_notify_marker_forces():
    h = delivery.result_hash("[NOTIFY]\nimportant!")
    j = _J(h)
    # Same hash but marked → still delivered (explicit interrupt request).
    assert delivery.should_deliver(j, "[NOTIFY]\nimportant!") is True
    assert delivery.should_deliver(_J(None), "[URGENT]\nfire") is True


def test_strip_marker():
    assert delivery.strip_marker("[NOTIFY]\nhello") == "hello"
    assert delivery.strip_marker("[URGENT]\n\nhi") == "hi"
    assert delivery.strip_marker("plain text") == "plain text"


def test_sinks():
    seen = []
    delivery.register_sink("test-sink", lambda arg, text: seen.append((arg, text)))
    delivery.deliver("test-sink:abc", "hi")
    assert seen == [("abc", "hi")]
    delivery.deliver("console", "to console")  # default sink exists
    with pytest.raises(DeliveryError):
        delivery.deliver("nope", "x")
    with pytest.raises(DeliveryError):
        delivery.deliver("", "x")
    with pytest.raises(ValueError):
        delivery.register_sink("", lambda a, t: None)


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------

def test_hook_crud(tmp_path):
    h = add_hook("/tmp/x", "file changed!", base=tmp_path)
    assert h.id
    assert [x.id for x in list_hooks(base=tmp_path)] == [h.id]
    assert remove_hook(h.id, base=tmp_path) is True
    assert remove_hook(h.id, base=tmp_path) is False
    with pytest.raises(ValueError):
        add_hook("", "x", base=tmp_path)


def test_hook_watcher_fires_on_change(tmp_path):
    target = tmp_path / "watched.txt"
    target.write_text("v1")
    add_hook(str(target), "changed!", base=tmp_path)

    w = HookWatcher(poll_s=0.05, base=tmp_path)
    w.start()
    try:
        import time
        time.sleep(0.15)
        assert w.drain() == []  # baseline: no fire on first sight
        target.write_text("v2")
        deadline = time.time() + 5
        fired = []
        while time.time() < deadline and not fired:
            time.sleep(0.1)
            fired = w.drain()
        assert len(fired) == 1 and fired[0].path == str(target)
        assert w.drain() == []  # drained clears
    finally:
        w.stop()


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------

class _FakeLoop:
    def __init__(self, text="done"):
        self._text = text

    def run(self, event):
        return RunResult(final_text=self._text, iterations=1,
                         tool_calls_made=0, stop_reason="done")


def test_worker_submit_result():
    pool = workers.WorkerPool(lambda: _FakeLoop("hi"))
    wid = pool.submit(events.user_message("task"))
    import time
    deadline = time.time() + 5
    while not pool.done(wid) and time.time() < deadline:
        time.sleep(0.05)
    assert pool.done(wid)
    res = pool.result(wid)
    assert res.final_text == "hi" and res.stop_reason == "done"
    assert pool.pending_ids() == []  # forgotten after collect
    with pytest.raises(KeyError):
        pool.done("nope")


def test_worker_captures_exception():
    def boom():
        raise RuntimeError("kaboom")

    class _BoomLoop:
        def run(self, event):
            return boom()

    pool = workers.WorkerPool(_BoomLoop)
    wid = pool.submit(events.user_message("task"))
    import time
    deadline = time.time() + 5
    while not pool.done(wid) and time.time() < deadline:
        time.sleep(0.05)
    res = pool.result(wid)
    assert res.stop_reason == "worker_error" and "kaboom" in res.final_text


# ---------------------------------------------------------------------------
# schedule tool
# ---------------------------------------------------------------------------

def test_delivery_error_never_leaks_target_pii():
    # Phone numbers are PII: the error names the channel, not the target.
    # ("pigeon" has no sink in any test process — deterministic.)
    with pytest.raises(DeliveryError) as exc:
        delivery.deliver("pigeon:+15551234567", "hello")
    assert "+15551234567" not in str(exc.value)
    assert "pigeon" in str(exc.value)


def test_schedule_tool_creates_job(tmp_path, monkeypatch):
    import prana.proactivity.jobs as jm
    orig_jobs_dir = jm.jobs_dir
    monkeypatch.setattr(jm, "jobs_dir", lambda base=None: orig_jobs_dir(tmp_path))
    # create_job writes via jobs_dir() → now redirected to tmp_path/jobs.
    reg = ToolRegistry(approver=lambda n, a: True)
    for t in builtin_schedule_tools():
        reg.register(t)
    res = reg.dispatch(ToolCall(id="s1", name="schedule", arguments={
        "name": "standup", "schedule": "every 1d", "request": "write standup",
    }))
    assert not res.is_error, res.output
    assert "standup" in res.output and "job id" in res.output
    assert len(jm.list_jobs(base=tmp_path)) == 1


def test_schedule_tool_is_irreversible():
    """Creating autonomous background jobs must always ask first."""
    from prana.tools.base import SideEffect
    from prana.tools.schedule_tool import builtin_schedule_tools
    tools = {t.name: t for t in builtin_schedule_tools()}
    assert tools["schedule"].side_effect == SideEffect.IRREVERSIBLE


def test_schedule_tool_denied_without_approval(tmp_path, monkeypatch):
    import prana.proactivity.jobs as jm
    orig_jobs_dir = jm.jobs_dir
    monkeypatch.setattr(jm, "jobs_dir", lambda base=None: orig_jobs_dir(tmp_path))
    reg = ToolRegistry(approver=lambda n, a: False)  # user says no
    for t in builtin_schedule_tools():
        reg.register(t)
    res = reg.dispatch(ToolCall(id="s1", name="schedule", arguments={
        "name": "sneaky", "schedule": "every 1d", "request": "do evil",
    }))
    assert res.is_error and "denied" in res.output
    assert jm.list_jobs(base=tmp_path) == []  # nothing was created


def test_parse_schedule_rejects_absurd_interval():
    # Audit repro: this used to OverflowError in next_due; now ValueError at parse.
    with pytest.raises(ValueError, match="too large"):
        scheduler.parse_schedule("every 999999999999999999999d")
    with pytest.raises(ValueError, match="too large"):
        scheduler.parse_schedule("every 9999999999999s")


def test_schedule_tool_rejects_bad_schedule():
    reg = ToolRegistry(approver=lambda n, a: True)
    for t in builtin_schedule_tools():
        reg.register(t)
    res = reg.dispatch(ToolCall(id="s1", name="schedule", arguments={
        "name": "x", "schedule": "whenever", "request": "y",
    }))
    assert res.is_error and "invalid schedule" in res.output


# ---------------------------------------------------------------------------
# daemon tick: scripted end-to-end of the proactivity loop
# ---------------------------------------------------------------------------

class _ScriptedLoop:
    """Fake AgentLoop: returns canned final text, records events seen."""

    def __init__(self, final_text="all quiet"):
        self.final_text = final_text
        self.seen = []

    def run(self, event):
        self.seen.append(event)
        return RunResult(final_text=self.final_text, iterations=1,
                         tool_calls_made=0, stop_reason="done")


def test_daemon_tick_delivers_and_updates_job(tmp_path):
    from prana.proactivity import daemon as dm
    from prana.proactivity import jobs as jm

    jm.create_job("ping", "every 1m", "say hi", base=tmp_path)
    loop = _ScriptedLoop("hello from the daemon")
    pool = workers.WorkerPool(lambda: loop)
    watcher = HookWatcher(poll_s=60, base=tmp_path)  # idle watcher
    watcher.start()
    try:
        delivered = []
        delivery.register_sink("test-daemon", lambda a, t: delivered.append(t))
        # point the job at our test sink
        job = jm.list_jobs(base=tmp_path)[0]
        job.delivery_target = "test-daemon:x"
        jm.save_job(job, base=tmp_path)

        now = utc(2026, 10, 18, 12, 0)
        summary = dm.tick_once(loop, pool, watcher, now,
                               jobs_base=tmp_path, hooks_base=tmp_path)
        assert summary["jobs_due"] == 1 and summary["delivered"] == 1
        assert delivered == ["hello from the daemon"]
        # cron_tick event actually flowed through the loop:
        assert loop.seen and loop.seen[0].kind == "cron_tick"
        assert loop.seen[0].meta["job_name"] == "ping"

        # Second tick, same result → silent (hash unchanged).
        delivered.clear()
        summary2 = dm.tick_once(loop, pool, watcher, now + timedelta(minutes=2),
                                 jobs_base=tmp_path, hooks_base=tmp_path)
        assert summary2["silent"] == 1 and delivered == []
    finally:
        watcher.stop()


def test_daemon_tick_once_stop_condition(tmp_path):
    from prana.proactivity import daemon as dm
    from prana.proactivity import jobs as jm

    jm.create_job("one-shot", "every 1m", "do once",
                  stop_condition="once", base=tmp_path)
    loop = _ScriptedLoop("[NOTIFY]\nimportant!")
    pool = workers.WorkerPool(lambda: loop)
    watcher = HookWatcher(poll_s=60, base=tmp_path)
    watcher.start()
    try:
        delivered = []
        delivery.register_sink("test-once", lambda a, t: delivered.append(t))
        job = jm.list_jobs(base=tmp_path)[0]
        job.delivery_target = "test-once:x"
        jm.save_job(job, base=tmp_path)

        dm.tick_once(loop, pool, watcher, utc(2026, 10, 18, 12, 0),
                     jobs_base=tmp_path, hooks_base=tmp_path)
        # [NOTIFY] marker stripped before delivery:
        assert delivered == ["important!"]
        # stop_condition="once" deactivated the job:
        assert jm.load_job(job.id, base=tmp_path).active is False
    finally:
        watcher.stop()


# -- daemon entry point (P5: console script wiring) --------------------------
def test_daemon_argv_defaults():
    from prana.proactivity import daemon as dm
    assert dm._parse_daemon_argv([]) == dm.TICK_INTERVAL_S


def test_daemon_argv_custom_interval():
    from prana.proactivity import daemon as dm
    assert dm._parse_daemon_argv(["--interval", "10"]) == 10.0


def test_daemon_main_no_key_exits_2(capsys, monkeypatch):
    # Provider build failure -> clean exit 2, no traceback, no hang.
    from prana.proactivity import daemon as dm
    from prana.providers.base import ProviderError
    monkeypatch.setattr(dm, "_build_daemon_agent",
                        lambda: (_ for _ in ()).throw(ProviderError("no key")))
    rc = dm.main(["--interval", "0.01"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "prana daemon" in err


def test_daemon_import_registers_telegram_sink():
    # Regression: importing the daemon must wire the telegram sink, or
    # jobs with delivery_target="telegram:<chat_id>" can never deliver.
    from prana.proactivity import daemon  # noqa: F401  (import = wiring)
    from prana.proactivity import delivery as d
    assert "telegram" in d._sinks
    assert "console" in d._sinks
