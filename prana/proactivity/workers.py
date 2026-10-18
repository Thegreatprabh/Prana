"""Background workers — long tasks that must not block the daemon tick.

A worker runs one :class:`prana.loop.AgentLoop` in a background thread.
The daemon polls :meth:`WorkerPool.done`; finished results come back as
``worker_result`` events that flow through the main loop like everything
else. Each worker gets a FRESH loop from the factory — loops are not
shared across threads.
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable, Dict, List, Optional

from ..events import Event
from ..loop import AgentLoop, RunResult


class WorkerPool:
    """Thread pool for AgentLoop runs. Results are collected, never lost."""

    def __init__(self, loop_factory: Callable[[], AgentLoop]) -> None:
        if not callable(loop_factory):
            raise ValueError("loop_factory must be callable")
        self._factory = loop_factory
        self._lock = threading.Lock()
        # worker_id -> {"thread": Thread, "done": bool, "result": Optional[RunResult]}
        self._workers: Dict[str, dict] = {}

    # ---- submit / query ----

    def submit(self, event: Event) -> str:
        """Start a worker on `event`. Returns the worker id."""
        wid = uuid.uuid4().hex[:12]
        state: dict = {"done": False, "result": None, "thread": None}

        def _run() -> None:
            try:
                loop = self._factory()
                result = loop.run(event)
            except Exception as e:  # never lose a worker silently
                result = RunResult(
                    final_text=f"Worker failed: {type(e).__name__}: {e}",
                    iterations=0,
                    tool_calls_made=0,
                    stop_reason="worker_error",
                )
            with self._lock:
                state["result"] = result
                state["done"] = True

        thread = threading.Thread(target=_run, name=f"prana-worker-{wid}", daemon=True)
        state["thread"] = thread
        with self._lock:
            self._workers[wid] = state
        thread.start()
        return wid

    def pending_ids(self) -> List[str]:
        """Ids of workers not yet collected (running or finished)."""
        with self._lock:
            return list(self._workers.keys())

    def done(self, worker_id: str) -> bool:
        """True when the worker finished. Raises KeyError on unknown id."""
        with self._lock:
            try:
                return bool(self._workers[worker_id]["done"])
            except KeyError:
                raise KeyError(f"unknown worker: {worker_id}")

    def result(self, worker_id: str) -> Optional[RunResult]:
        """Return the result and FORGET the worker. None while still running."""
        with self._lock:
            try:
                state = self._workers[worker_id]
            except KeyError:
                raise KeyError(f"unknown worker: {worker_id}")
            if not state["done"]:
                return None
            del self._workers[worker_id]
            return state["result"]

    # ---- shutdown ----

    def shutdown(self, wait: bool = True, timeout: Optional[float] = None) -> None:
        """Stop accepting work; optionally wait for running workers."""
        with self._lock:
            threads = [s["thread"] for s in self._workers.values()]
        if wait:
            for t in threads:
                t.join(timeout=timeout)


__all__ = ["WorkerPool"]
