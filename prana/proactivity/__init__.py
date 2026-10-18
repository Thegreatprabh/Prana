"""PRANA proactivity engine — the "alive" part (SPEC.md §2.4).

The scheduler, hooks, workers, and delivery decision that let PRANA act
WITHOUT a user prompt. The daemon (``prana.proactivity.daemon``) is the
runtime; it is imported explicitly, not here, so this package stays
importable without the CLI wiring.
"""

from . import delivery, hooks, jobs, scheduler, workers

__all__ = ["delivery", "hooks", "jobs", "scheduler", "workers"]
