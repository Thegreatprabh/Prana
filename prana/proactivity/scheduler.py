"""Minimal cron scheduler — stdlib only.

Supported specs:
- Classic five-field cron: ``* * * * *`` (minute hour dom month dow).
  Fields accept ``*``, ``*/step``, ``a-b``, ``a-b/step``, ``a,b,c``.
  Day-of-week: 0–6, Sunday = 0 (7 is also accepted as Sunday).
- Shorthands: ``@hourly`` (``0 * * * *``), ``@daily`` (``0 0 * * *``),
  ``@weekly`` (``0 0 * * 0``), ``@monthly`` (``0 0 1 * *``),
  ``@yearly`` (``0 0 1 1 *``).
- Intervals: ``every 30s``, ``every 15m``, ``every 2h``, ``every 1d``.

All datetimes are UTC. Cron day-of-month and day-of-week use AND
semantics (both must match when both are restricted) — documented here
because classic cron uses OR; AND is simpler and predictable for our
use, and the common shapes (``@daily`` etc.) are unaffected.

``is_due`` never fires twice for the same minute: a job whose
``last_run`` falls inside the currently-matching minute is not due.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import FrozenSet, List, Optional

from .jobs import Job

_FIELD_RANGES = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0 = Sunday)
)

_SHORTHANDS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}

_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([smhd])$", re.IGNORECASE)
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: Intervals above this are rejected at parse time (datetime would overflow).
_MAX_INTERVAL_S = 100 * 365 * 24 * 3600  # 100 years in seconds


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_field(text: str, lo: int, hi: int, is_dow: bool = False) -> FrozenSet[int]:
    vals = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field part in {text!r}")
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"bad cron step: {part!r}")
            if step <= 0:
                raise ValueError(f"cron step must be > 0: {part!r}")
            if base in ("*", ""):
                start, end = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                start, end = int(a), int(b)
            else:
                start, end = int(base), hi
            vals.update(range(start, end + 1, step))
        elif part == "*":
            vals.update(range(lo, hi + 1))
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                raise ValueError(f"reversed cron range: {part!r}")
            vals.update(range(start, end + 1))
        else:
            vals.add(int(part))
    out = set()
    for v in vals:
        if is_dow and v == 7:
            v = 0  # Sunday alias
        if not (lo <= v <= hi):
            raise ValueError(f"cron value {v} out of range [{lo}-{hi}]")
        out.add(v)
    if not out:
        raise ValueError(f"empty cron field: {text!r}")
    return frozenset(out)


@dataclass(frozen=True)
class CronSpec:
    minute: FrozenSet[int]
    hour: FrozenSet[int]
    dom: FrozenSet[int]
    month: FrozenSet[int]
    dow: FrozenSet[int]  # Monday=0..Sunday=6 (Python weekday); converted at match


@dataclass(frozen=True)
class Schedule:
    kind: str  # "cron" | "interval"
    cron: Optional[CronSpec]
    interval_s: Optional[int]
    raw: str


def parse_schedule(spec: str) -> Schedule:
    """Parse a schedule spec. Raises ValueError on anything invalid."""
    if not spec or not spec.strip():
        raise ValueError("empty schedule")
    text = spec.strip()

    low = text.lower()
    if low in _SHORTHANDS:
        text = _SHORTHANDS[low]
    m = _EVERY_RE.match(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        if amount <= 0:
            raise ValueError(f"interval must be > 0: {spec!r}")
        interval_s = amount * _UNIT_S[unit]
        # Clamp absurd intervals: anything over 100 years would overflow
        # datetime arithmetic downstream (OverflowError instead of ValueError).
        if interval_s > _MAX_INTERVAL_S:
            raise ValueError(f"interval too large (max 100 years): {spec!r}")
        return Schedule(
            kind="interval", cron=None,
            interval_s=interval_s, raw=spec.strip(),
        )

    parts = text.split()
    if len(parts) != 5:
        raise ValueError(
            f"bad cron spec {spec!r}: want 5 fields (min hour dom mon dow), "
            f"@hourly/@daily/@weekly/@monthly/@yearly, or 'every <N>s|m|h|d'"
        )
    try:
        fields = [
            _parse_field(p, lo, hi, is_dow=(i == 4))
            for i, (p, (lo, hi)) in enumerate(zip(parts, _FIELD_RANGES))
        ]
    except ValueError as e:
        raise ValueError(f"bad cron spec {spec!r}: {e}")
    cron = CronSpec(*fields)  # type: ignore[arg-type]
    return Schedule(kind="cron", cron=cron, interval_s=None, raw=spec.strip())


def matches_cron(cron: CronSpec, dt: datetime) -> bool:
    """True when `dt` (UTC) matches every cron field."""
    dt = _ensure_utc(dt)
    # Python weekday(): Monday=0..Sunday=6 → cron Sunday=0.
    cron_dow = (dt.weekday() + 1) % 7
    return (
        dt.minute in cron.minute
        and dt.hour in cron.hour
        and dt.day in cron.dom
        and dt.month in cron.month
        and cron_dow in cron.dow
    )


def is_due(schedule: Schedule, now: datetime, last_run: Optional[datetime]) -> bool:
    """True when the schedule should fire at `now`.

    Interval: due when never run, or the interval has fully elapsed.
    Cron: due when `now` matches AND the job did not already run inside
    this matching minute (prevents double-firing on fast ticks).
    """
    now = _ensure_utc(now)
    last = _ensure_utc(last_run) if last_run is not None else None

    if schedule.kind == "interval":
        assert schedule.interval_s is not None
        if last is None:
            return True
        return (now - last).total_seconds() >= schedule.interval_s

    assert schedule.cron is not None
    if not matches_cron(schedule.cron, now):
        return False
    if last is None:
        return True
    this_minute = now.replace(second=0, microsecond=0)
    return last < this_minute


def next_due(
    schedule: Schedule, after: datetime, last_run: Optional[datetime] = None
) -> Optional[datetime]:
    """Next firing time strictly after `after`. None only if none within a year."""
    after = _ensure_utc(after)
    if schedule.kind == "interval":
        assert schedule.interval_s is not None
        base = _ensure_utc(last_run) if last_run is not None else after
        nxt = base + timedelta(seconds=schedule.interval_s)
        while nxt <= after:
            nxt += timedelta(seconds=schedule.interval_s)
        return nxt
    assert schedule.cron is not None
    probe = (after.replace(second=0, microsecond=0) + timedelta(minutes=1))
    for _ in range(366 * 24 * 60):  # one year cap
        if matches_cron(schedule.cron, probe):
            return probe
        probe += timedelta(minutes=1)
    return None


def due_jobs(jobs: List[Job], now: datetime) -> List[Job]:
    """Active jobs whose schedule is due at `now`. Invalid specs are skipped."""
    now = _ensure_utc(now)
    out: List[Job] = []
    for job in jobs:
        if not job.active:
            continue
        try:
            sched = parse_schedule(job.schedule)
        except ValueError:
            continue  # corrupt spec: skip, never crash the daemon tick
        if is_due(sched, now, job.last_run_dt()):
            out.append(job)
    return out
