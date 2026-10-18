"""The `schedule` tool — create cron jobs from inside the agent.

Deferred from P1 (SPEC.md §2.2): the memory-write (P2) and cron-create
primitives were held back rather than shipped as stubs. This is the real
one — it creates a persistent job that the PRANA daemon
(``prana.proactivity.daemon``) picks up on its ticks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from .base import SideEffect, Tool


def _do_schedule(
    name: str,
    schedule: str,
    request: str,
    delivery_target: str = "console",
    owner: str = "user",
) -> str:
    # Lazy imports keep the tools <-> proactivity import graph cycle-free.
    from ..proactivity import jobs as job_store
    from ..proactivity import scheduler as sched

    try:
        parsed = sched.parse_schedule(schedule)
    except ValueError as e:
        raise ValueError(f"invalid schedule: {e}")
    job = job_store.create_job(
        name=name.strip(),
        schedule=schedule.strip(),
        request=request,
        delivery_target=delivery_target,
        owner=owner,
    )
    nxt = sched.next_due(parsed, datetime.now(timezone.utc))
    when = f"next run ~{nxt.isoformat()}" if nxt else "next run: unknown"
    return (
        f"scheduled '{job.name}' → job id {job.id}\n"
        f"schedule: {schedule.strip()} ({when})\n"
        f"delivery: {delivery_target}\n"
        f"The PRANA daemon runs due jobs: python -m prana.proactivity.daemon"
    )


_SCHEDULE_PARAMS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "short human name for the job, e.g. 'morning digest'",
        },
        "schedule": {
            "type": "string",
            "description": (
                "when to run: cron '30 9 * * *', @hourly/@daily/@weekly, "
                "or 'every 30m' / 'every 2h' / 'every 1d'"
            ),
        },
        "request": {
            "type": "string",
            "description": "the task the agent works on at every tick",
        },
        "delivery_target": {
            "type": "string",
            "description": "where results go: 'console' (default) or 'telegram:CHAT_ID'",
        },
    },
    "required": ["name", "schedule", "request"],
}


def builtin_schedule_tools() -> List[Tool]:
    return [
        Tool(
            name="schedule",
            description=(
                "Create a scheduled job that the PRANA daemon runs on its own, "
                "without a user prompt. Creating a job needs the user's "
                "approval (it acts autonomously later). The daemon works the "
                "`request` task at each tick and messages the user only when "
                "the result is new or marked [NOTIFY]. Use for reminders, "
                "periodic checks, digests."
            ),
            parameters=_SCHEDULE_PARAMS,
            side_effect=SideEffect.IRREVERSIBLE,
            func=_do_schedule,
        )
    ]


__all__ = ["builtin_schedule_tools"]
