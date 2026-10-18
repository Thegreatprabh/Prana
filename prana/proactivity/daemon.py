"""PRANA daemon — the proactivity runtime.

This is the process that makes PRANA feel alive: it wakes on its own
schedule, works tasks without a user prompt, and messages the user only
when something is genuinely worth their attention (see delivery.py).

Entry point: ``python -m prana.proactivity.daemon`` or the ``prana-daemon``
console script.

One daemon tick:
1. Find due jobs → build a ``cron_tick`` event per job → run the agent loop
   → delivery decision → deliver or stay silent → persist job state.
2. Drain fired hooks → ``hook_event`` → loop → delivery decision.
3. Collect finished workers → ``worker_result`` → loop → delivery decision.

The agent loop and provider are built once and reused for daemon-driven
runs; background workers each get a FRESH loop from the factory (loops
are not shared across threads).

Graceful Ctrl+C: stops the hook watcher, shuts down workers, exits 0.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

from .. import events
from ..loop import AgentLoop
from ..providers.base import ProviderError
from . import delivery, hooks as hookmod, jobs, scheduler, workers
# Imported for its side effect: registers the "telegram" sink with the
# delivery registry above, so jobs with delivery_target="telegram:<chat_id>"
# actually reach the phone. (channels.notify itself stays decoupled via a
# guarded import — no cycle.)
from ..channels import notify as _channel_notify  # noqa: F401

#: How often the daemon wakes to check schedules/hooks/workers.
TICK_INTERVAL_S = 30.0

#: Appended to every scheduled/hook task so the model knows it may stay
#: silent — and how to explicitly request an interruption.
NOTIFY_HINT = (
    "\n\n[PRANA context: this is an autonomous run — no user is waiting. "
    "Do the task above with your tools. If the outcome is genuinely worth "
    "interrupting the user for, start your final answer with [NOTIFY] on its "
    "own first line. Otherwise answer normally; routine or unchanged results "
    "are delivered silently and you should keep the answer brief.]"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ephemeral_job() -> SimpleNamespace:
    """A throwaway stand-in for should_deliver (first-run semantics)."""
    return SimpleNamespace(last_result_hash=None)


# ---------------------------------------------------------------------------
# One unit of autonomous work
# ---------------------------------------------------------------------------

def run_scheduled_job(
    loop: AgentLoop,
    job: jobs.Job,
    now: datetime,
    jobs_base: Optional[Path] = None,
) -> str:
    """Run one due job through the loop; deliver or stay silent.

    Returns "delivered" or "silent". Persists job state (last_run,
    last_result_hash, and deactivation for stop_condition="once").
    Never raises on delivery-sink errors — they are logged, not fatal.
    """
    event = events.cron_tick(
        job.request + NOTIFY_HINT,
        job_id=job.id,
        job_name=job.name,
    )
    result = loop.run(event)
    text = (result.final_text or "").strip()

    outcome = "silent"
    if delivery.should_deliver(job, text):
        try:
            delivery.deliver(job.delivery_target, delivery.strip_marker(text))
            outcome = "delivered"
        except Exception as e:  # sink errors (network, auth, config) must never kill the daemon
            print(f"prana daemon: delivery failed for job {job.name!r}: {e}",
                  file=sys.stderr)

    job.last_run = now.isoformat()
    job.last_result_hash = delivery.result_hash(text) if text else job.last_result_hash
    if job.stop_condition == "once":
        job.active = False
    jobs.save_job(job, jobs_base)
    return outcome


def run_hook_fired(
    loop: AgentLoop,
    fired: hookmod.HookFired,
    hooks_base: Optional[Path] = None,
) -> str:
    """Run one fired hook through the loop; deliver or stay silent."""
    hook = fired.hook
    event = events.hook_event(
        hook.event_text + NOTIFY_HINT,
        hook_id=hook.id,
        path=fired.path,
    )
    result = loop.run(event)
    text = (result.final_text or "").strip()

    outcome = "silent"
    if delivery.should_deliver(hook, text):
        try:
            delivery.deliver(hook.delivery_target, delivery.strip_marker(text))
            outcome = "delivered"
        except Exception as e:  # sink errors (network, auth, config) must never kill the daemon
            print(f"prana daemon: delivery failed for hook {hook.id!r}: {e}",
                  file=sys.stderr)

    if text:
        hook.last_result_hash = delivery.result_hash(text)
    hookmod.update_hook(hook, hooks_base)
    return outcome


def run_worker_collected(
    loop: AgentLoop,
    worker_id: str,
    worker_text: str,
) -> str:
    """Feed a finished worker's result through the loop; deliver or stay silent."""
    event = events.worker_result(
        worker_text + NOTIFY_HINT,
        worker_id=worker_id,
    )
    result = loop.run(event)
    text = (result.final_text or "").strip()
    if delivery.should_deliver(_ephemeral_job(), text):
        try:
            delivery.deliver("console", delivery.strip_marker(text))
            return "delivered"
        except Exception as e:  # sink errors (network, auth, config) must never kill the daemon
            print(f"prana daemon: delivery failed for worker {worker_id}: {e}",
                  file=sys.stderr)
    return "silent"


# ---------------------------------------------------------------------------
# One daemon tick (testable without a running daemon)
# ---------------------------------------------------------------------------

def tick_once(
    loop: AgentLoop,
    pool: workers.WorkerPool,
    watcher: hookmod.HookWatcher,
    now: datetime,
    jobs_base: Optional[Path] = None,
    hooks_base: Optional[Path] = None,
) -> Dict[str, int]:
    """Run a single daemon iteration. Returns a summary dict."""
    summary = {"jobs_due": 0, "delivered": 0, "silent": 0,
               "hooks": 0, "workers": 0}

    for job in jobs.list_jobs(jobs_base):
        if not job.active:
            continue
        try:
            sched = scheduler.parse_schedule(job.schedule)
        except ValueError as e:
            print(f"prana daemon: skipping job {job.name!r}: bad schedule: {e}",
                  file=sys.stderr)
            continue
        if scheduler.is_due(sched, now, job.last_run_dt()):
            summary["jobs_due"] += 1
            print(f"prana daemon: running job {job.name!r}", file=sys.stderr)
            outcome = run_scheduled_job(loop, job, now, jobs_base)
            summary[outcome] += 1
            print(f"prana daemon: job {job.name!r} → {outcome}", file=sys.stderr)

    for fired in watcher.drain():
        summary["hooks"] += 1
        print(f"prana daemon: hook fired: {fired.path}", file=sys.stderr)
        outcome = run_hook_fired(loop, fired, hooks_base)
        summary[outcome] += 1

    for wid in pool.pending_ids():
        if pool.done(wid):
            wres = pool.result(wid)
            summary["workers"] += 1
            outcome = run_worker_collected(
                loop, wid, (wres.final_text if wres else "") or "")
            summary[outcome] += 1

    return summary


# ---------------------------------------------------------------------------
# Daemon entry point
# ---------------------------------------------------------------------------

def _build_daemon_agent() -> AgentLoop:
    """Build the agent loop the daemon drives. Reuses the CLI wiring."""
    # Imported here so `prana.proactivity` stays importable without the CLI.
    from prana.__main__ import build_agent
    args = argparse.Namespace(
        key=None, provider=None, model=None,
        max_iters=25, identity=None, sandbox=os.getcwd(),
    )
    loop, _provider = build_agent(args)
    return loop


def _parse_daemon_argv(argv=None) -> float:
    p = argparse.ArgumentParser(
        prog="prana-daemon",
        description="PRANA proactivity daemon — runs scheduled jobs, hooks and "
                    "workers on their own, messaging you only when something is "
                    "worth your attention.",
    )
    p.add_argument("--interval", type=float, default=TICK_INTERVAL_S,
                   help=f"seconds between ticks (default: {TICK_INTERVAL_S:g})")
    return p.parse_args(argv).interval


def main(argv=None) -> int:
    """Run the daemon forever. Ctrl+C stops gracefully."""
    interval_s = _parse_daemon_argv(argv)
    try:
        loop = _build_daemon_agent()
    except ProviderError as e:
        print(f"prana daemon: {e}", file=sys.stderr)
        return 2

    def factory() -> AgentLoop:
        return _build_daemon_agent()

    pool = workers.WorkerPool(factory)
    watcher = hookmod.HookWatcher()
    watcher.start()

    print(f"prana daemon running (tick {interval_s:g}s) — Ctrl+C to stop",
          file=sys.stderr)
    try:
        while True:
            tick_once(loop, pool, watcher, utcnow())
            time.sleep(interval_s)
    except KeyboardInterrupt:
        print("\nprana daemon stopped", file=sys.stderr)
    finally:
        watcher.stop()
        pool.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
