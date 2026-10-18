"""Scheduled jobs: model + JSON persistence.

One JSON file per job under ``~/.prana/jobs/<id>.json`` with 0600
permissions — job files hold the user's own task text, so they are
treated like credentials. This module is pure storage + CRUD;
:mod:`prana.proactivity.scheduler` decides *when* a job is due,
:mod:`prana.proactivity.delivery` decides *whether* a result is worth
sending, and :mod:`prana.proactivity.daemon` *runs* everything.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .. import config

JOBS_DIR_NAME = "jobs"


def jobs_dir(base: Optional[Path] = None) -> Path:
    """Return the jobs directory (0700), creating it if needed."""
    d = Path(base) if base is not None else config.get_config_dir()
    d = d / JOBS_DIR_NAME
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically with 0600 perms (job text is user-private)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
        os.chmod(path, 0o600)


@dataclass
class Job:
    """One scheduled unit of work."""

    id: str
    name: str
    # Schedule spec — see scheduler.parse_schedule: "* * * * *",
    # "@daily", or "every 30m".
    schedule: str
    # The task text the agent works on at every tick.
    request: str
    owner: str = "user"
    # None (run forever) or "once" (deactivate after the first run).
    stop_condition: Optional[str] = None
    # "console" | "telegram:CHAT_ID" | ... (see delivery.register_sink)
    delivery_target: str = "console"
    active: bool = True
    created_at: str = field(default_factory=_utcnow_iso)
    last_run: Optional[str] = None  # ISO datetime of the last tick
    last_result_hash: Optional[str] = None  # sha256 of the last result text

    def last_run_dt(self) -> Optional[datetime]:
        return _parse_iso(self.last_run)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)  # type: ignore[arg-type]


def _path(job_id: str, base: Optional[Path] = None) -> Path:
    # IDs are hex tokens we mint ourselves; still, reject path separators.
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        raise ValueError(f"bad job id: {job_id!r}")
    return jobs_dir(base) / f"{job_id}.json"


def create_job(
    name: str,
    schedule: str,
    request: str,
    *,
    owner: str = "user",
    stop_condition: Optional[str] = None,
    delivery_target: str = "console",
    base: Optional[Path] = None,
) -> Job:
    """Create and persist a job. Raises ValueError on empty fields."""
    if not name or not name.strip():
        raise ValueError("job name must not be empty")
    if not schedule or not schedule.strip():
        raise ValueError("job schedule must not be empty")
    if not request or not request.strip():
        raise ValueError("job request must not be empty")
    job = Job(
        id=uuid.uuid4().hex[:12],
        name=name.strip(),
        schedule=schedule.strip(),
        request=request,
        owner=owner,
        stop_condition=stop_condition,
        delivery_target=delivery_target,
    )
    save_job(job, base)
    return job


def save_job(job: Job, base: Optional[Path] = None) -> Path:
    """Persist a job (0600). Returns the path written."""
    path = _path(job.id, base)
    _atomic_write_json(path, job.to_dict())
    return path


def load_job(job_id: str, base: Optional[Path] = None) -> Optional[Job]:
    """Load one job; None when it does not exist or is unreadable."""
    try:
        data = json.loads(_path(job_id, base).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Job.from_dict(data)
    except TypeError:
        return None


def list_jobs(base: Optional[Path] = None) -> List[Job]:
    """All jobs, sorted by name. Corrupt files are skipped, never raised."""
    out: List[Job] = []
    try:
        files = sorted(jobs_dir(base).glob("*.json"))
    except OSError:
        return []
    for path in files:
        job = load_job(path.stem, base)
        if job is not None:
            out.append(job)
    out.sort(key=lambda j: j.name.lower())
    return out


def remove_job(job_id: str, base: Optional[Path] = None) -> bool:
    """Delete a job. True when something was removed."""
    try:
        _path(job_id, base).unlink()
        return True
    except (OSError, ValueError):
        return False


def set_active(job_id: str, active: bool, base: Optional[Path] = None) -> Optional[Job]:
    """Enable/disable a job. None when the job does not exist."""
    job = load_job(job_id, base)
    if job is None:
        return None
    job.active = active
    save_job(job, base)
    return job
