"""Local configuration: keys, settings, identity path.

Everything lives under ``~/.prana/``. The config file holds API keys, so it
is created with 0600 permissions and we re-enforce that on every load.
Keys NEVER appear in logs, errors, or prompts — see providers/* for the
redaction discipline.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_DIR_NAME = ".prana"
CONFIG_FILE_NAME = "config.json"
IDENTITY_FILE_NAME = "SOUL.md"

#: env vars checked (in order) for each provider before falling back to config.
ENV_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


def get_config_dir() -> Path:
    """Return ~/.prana, creating it (0700) if needed."""
    d = Path.home() / CONFIG_DIR_NAME
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Harden perms in case it already existed with looser mode.
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _config_path() -> Path:
    return get_config_dir() / CONFIG_FILE_NAME


def load_config() -> Dict[str, Any]:
    """Load config dict; returns {} when missing or unreadable (never raises)."""
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_config(data: Dict[str, Any]) -> Path:
    """Write config atomically with 0600 perms. Returns the path written."""
    path = _config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    # Verify final perms (umask games shouldn't widen them).
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        os.chmod(path, 0o600)
    return path


def get_key(provider: str) -> Optional[str]:
    """Resolve an API key: explicit env vars first, then config file.

    Returns None when no key is configured. The value is returned as-is;
    callers must never log it.
    """
    for env in ENV_KEYS.get(provider, ()):
        val = os.environ.get(env)
        if val:
            return val
    keys = load_config().get("keys", {})
    if isinstance(keys, dict):
        val = keys.get(provider)
        if isinstance(val, str) and val.strip():
            return val
    return None


def set_key(provider: str, key: str) -> Path:
    """Persist an API key for a provider (0600). Returns config path."""
    data = load_config()
    keys = data.get("keys")
    if not isinstance(keys, dict):
        keys = {}
        data["keys"] = keys
    keys[provider] = key
    return save_config(data)


def identity_path() -> Path:
    """Default location of the SOUL.md-style identity file."""
    return get_config_dir() / IDENTITY_FILE_NAME


def get_channel_allowed_ids(channel: str) -> list:
    """Allowlisted sender ids for a channel (pairing). Empty = not paired yet."""
    data = load_config().get("channels", {})
    if not isinstance(data, dict):
        return []
    section = data.get(channel, {})
    if not isinstance(section, dict):
        return []
    ids = section.get("allowed_ids", [])
    return [str(i) for i in ids] if isinstance(ids, list) else []


def add_channel_allowed_id(channel: str, sender_id: str) -> None:
    """Append sender_id to the channel's allowlist (persisted, 0600).

    Refuses to run when an existing config file is unreadable — overwriting
    it would destroy the user's API keys.
    """
    path = _config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("config file unreadable — refusing to overwrite")
        if not isinstance(raw, dict):
            raise ValueError("config file unreadable — refusing to overwrite")
    data = load_config()
    channels = data.get("channels")
    if not isinstance(channels, dict):
        channels = {}
        data["channels"] = channels
    section = channels.get(channel)
    if not isinstance(section, dict):
        section = {}
        channels[channel] = section
    ids = section.get("allowed_ids")
    if not isinstance(ids, list):
        ids = []
        section["allowed_ids"] = ids
    sid = str(sender_id)
    if sid not in ids:
        ids.append(sid)
    save_config(data)


def default_identity() -> str:
    """Sane default identity used when no SOUL.md exists."""
    return (
        "# PRANA identity (default)\n\n"
        "You are PRANA, a helpful autonomous agent. You are direct, warm, and "
        "practical. You complete tasks using the tools available to you, verify "
        "your work, and report results concisely. You never claim work you did "
        "not do, and you ask for approval before any irreversible action."
    )
