"""File-watcher hooks — event-driven proactivity without a prompt.

A hook watches one path. When the path's mtime changes, the watcher
produces a payload the daemon feeds through the agent loop as a
``hook_event``. Polling (stdlib ``threading``) keeps this dependency-free;
inotify would be nicer and is a future upgrade.

Restart semantics: hook definitions (and their last-seen mtimes) persist
in ``~/.prana/hooks.json`` (0600). A hook seen for the first time only
records a baseline — it does not fire. After a daemon restart, changes
that happened while down DO fire, because the persisted mtime is stale.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .. import config

HOOKS_FILE_NAME = "hooks.json"


def hooks_file(base: Optional[Path] = None) -> Path:
    d = Path(base) if base is not None else config.get_config_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d / HOOKS_FILE_NAME


@dataclass
class Hook:
    """One watched path."""

    id: str
    path: str
    # The instruction text the agent works on when this hook fires.
    event_text: str
    active: bool = True
    delivery_target: str = "console"
    # Last-seen mtime; None until the first successful stat.
    last_mtime: Optional[float] = None
    # sha256 of the last hook result (delivery dedup, like jobs).
    last_result_hash: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Hook":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]


@dataclass
class HookFired:
    hook: Hook
    path: str


def _read_all(path: Path) -> List[Hook]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            out.append(Hook.from_dict(item))
        except TypeError:
            continue
    return out


def _write_all(path: Path, hooks: List[Hook]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps([h.to_dict() for h in hooks], indent=2), encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    if stat.S_IMODE(os.stat(path).st_mode) != 0o600:
        os.chmod(path, 0o600)


def list_hooks(base: Optional[Path] = None) -> List[Hook]:
    return _read_all(hooks_file(base))


def add_hook(
    path: str,
    event_text: str,
    *,
    delivery_target: str = "console",
    base: Optional[Path] = None,
) -> Hook:
    """Register a hook. Raises ValueError on empty fields."""
    if not path or not path.strip():
        raise ValueError("hook path must not be empty")
    if not event_text or not event_text.strip():
        raise ValueError("hook event_text must not be empty")
    hook = Hook(
        id=uuid.uuid4().hex[:12],
        path=path.strip(),
        event_text=event_text,
        delivery_target=delivery_target,
    )
    f = hooks_file(base)
    hooks = _read_all(f)
    hooks.append(hook)
    _write_all(f, hooks)
    return hook


def remove_hook(hook_id: str, base: Optional[Path] = None) -> bool:
    f = hooks_file(base)
    hooks = _read_all(f)
    kept = [h for h in hooks if h.id != hook_id]
    if len(kept) == len(hooks):
        return False
    _write_all(f, kept)
    return True


def update_hook(hook: Hook, base: Optional[Path] = None) -> bool:
    """Persist a mutated hook (e.g. last_result_hash). False when missing."""
    f = hooks_file(base)
    hooks = _read_all(f)
    for i, h in enumerate(hooks):
        if h.id == hook.id:
            hooks[i] = hook
            _write_all(f, hooks)
            return True
    return False


def _stat_mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class HookWatcher:
    """Polling watcher thread. `drain()` returns and clears fired events."""

    def __init__(self, poll_s: float = 5.0, base: Optional[Path] = None) -> None:
        if poll_s <= 0:
            raise ValueError("poll_s must be > 0")
        self._poll_s = poll_s
        self._base = base
        self._file = hooks_file(base)
        self._hooks: Dict[str, Hook] = {}
        self._pending: List[HookFired] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----

    def start(self) -> None:
        """Load hooks, record baselines silently, start polling."""
        with self._lock:
            self._hooks = {h.id: h for h in _read_all(self._file)}
            for hook in self._hooks.values():
                if hook.active and hook.last_mtime is None:
                    hook.last_mtime = _stat_mtime(hook.path)
            self._persist_locked()
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._poll_loop, name="prana-hooks", daemon=True
                )
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_s + 2)
            self._thread = None

    def reload(self) -> None:
        """Re-read hook definitions from disk (picks up add/remove)."""
        with self._lock:
            current = {h.id: h for h in _read_all(self._file)}
            for hid, hook in current.items():
                old = self._hooks.get(hid)
                # Preserve the running mtime baseline across reloads.
                if old is not None and old.last_mtime is not None:
                    hook.last_mtime = old.last_mtime
            self._hooks = current

    # ---- consumption ----

    def drain(self) -> List[HookFired]:
        """Return fired events since the last drain (clears the queue)."""
        with self._lock:
            fired = self._pending
            self._pending = []
            return fired

    # ---- internals ----

    def _persist_locked(self) -> None:
        _write_all(self._file, list(self._hooks.values()))

    def _poll_once(self) -> None:
        changed = False
        with self._lock:
            for hook in self._hooks.values():
                if not hook.active:
                    continue
                mtime = _stat_mtime(hook.path)
                if hook.last_mtime is None:
                    # First sighting: baseline silently, never fire.
                    hook.last_mtime = mtime
                    changed = True
                    continue
                if mtime is not None and mtime != hook.last_mtime:
                    hook.last_mtime = mtime
                    self._pending.append(HookFired(hook=hook, path=hook.path))
                    changed = True
                elif mtime is None and hook.last_mtime is not None:
                    # Watched path vanished: update baseline silently; a
                    # reappearing file will fire on its new mtime.
                    hook.last_mtime = None
                    changed = True
            if changed:
                self._persist_locked()

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                self._poll_once()
            except Exception:
                # A watcher must never die on a weird filesystem state.
                continue


__all__ = [
    "Hook",
    "HookFired",
    "HookWatcher",
    "add_hook",
    "remove_hook",
    "list_hooks",
    "hooks_file",
]
