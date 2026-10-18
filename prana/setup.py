"""`prana setup` — instant installer: API key + agent MCP configs + channels.

Writes MCP server entries so your AI agents can call PRANA's tools, saves
your BYOK API key, and prints the 2-minute human steps for chat channels.

Usage:
    python -m prana.setup                        # interactive: key + detected agents
    python -m prana.setup --key sk-... --yes     # non-interactive key + all agents
    python -m prana.setup --agents claude,cursor # only these agents
    python -m prana.setup --dry-run              # preview without writing
    python -m prana.setup --check                # verify installation health

The MCP server entry points at `python -m prana.mcp_server` using the FULL
python path — GUI apps (Claude Desktop, Cursor, ChatGPT Desktop) do not
inherit your terminal PATH, so a bare `python` would fail there.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config
from .providers.base import ProviderError
from .providers.detect import detect_provider

try:
    import tomllib  # Python 3.11+; absent on 3.10
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

SERVER_NAME = "prana"
MCP_MODULE = "prana.mcp_server"
MIN_PYTHON = (3, 10)


def server_command() -> List[str]:
    """Full-path python + MCP module. Never a bare `python` (GUI PATH issue)."""
    return [sys.executable, "-m", MCP_MODULE]


def mcp_server_shipped() -> bool:
    """Is prana.mcp_server importable in this install?"""
    return importlib.util.find_spec(MCP_MODULE) is not None


# ---------------------------------------------------------------------------
# API key step
# ---------------------------------------------------------------------------

def _key_hint(key: str) -> str:
    k = (key or "").strip()
    return (k[:4] + "…") if len(k) > 4 else "?"


def resolve_key_provider(key: str, provider: Optional[str]) -> str:
    """Provider for a key: explicit --provider wins, else sniff the prefix.

    Raises ProviderError when the prefix is unknown and no provider given.
    Never raises for a *valid* explicit provider.
    """
    from .providers.detect import resolve_provider, PROVIDERS

    key = (key or "").strip()
    if not key:
        raise ProviderError("empty API key — not saved")
    if provider:
        name = provider.strip().lower()
        if name not in PROVIDERS:
            raise ProviderError(
                f"unknown provider '{provider}' — choose from: "
                f"{', '.join(sorted(PROVIDERS))}"
            )
        return name
    return detect_provider(key)  # may raise ProviderError with safe hint


def save_api_key(key: str, provider: Optional[str] = None) -> Tuple[str, Path]:
    """Validate + persist an API key (0600). Returns (provider, path).

    The key value is never printed, logged, or included in errors.
    """
    name = resolve_key_provider(key, provider)
    path = config.set_key(name, key.strip())
    return name, path


def key_configured() -> Optional[str]:
    """Provider name with a saved key (env or config), else None."""
    for name in ("openai", "anthropic", "gemini"):
        if config.get_key(name):
            return name
    return None


def prompt_for_key() -> str:
    """Securely read a key from the terminal. The value never echoes."""
    try:
        return getpass.getpass("Paste API key (hidden, any provider): ").strip()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# Agent MCP configs
# ---------------------------------------------------------------------------

def _claude_paths() -> List[Path]:
    home = Path.home()
    paths = [home / ".claude.json"]
    if sys.platform == "darwin":
        paths.append(
            home / "Library" / "Application Support" / "Claude"
            / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    return paths


def _json_merge(path: Path) -> Tuple[Path, Dict[str, Any]]:
    """Merge the prana server into a JSON MCP config. Never clobbers."""
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # Corrupt file: back it up before starting fresh so the user's
            # other servers are never silently lost.
            try:
                bak = path.with_suffix(path.suffix + ".bak")
                bak.write_bytes(path.read_bytes())
                print(f"warning: {path} was unreadable; backed up to {bak}",
                      file=sys.stderr)
            except OSError:
                pass
            data = {}
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    cmd = server_command()
    servers[SERVER_NAME] = {"command": cmd[0], "args": cmd[1:]}
    return path, data


def _toml_append(path: Path) -> Tuple[Path, str]:
    """Append the prana server block to a TOML MCP config. Idempotent."""
    cmd = server_command()
    block = (
        f"\n[mcp_servers.{SERVER_NAME}]\n"
        f'command = "{cmd[0]}"\n'
        f"args = {json.dumps(cmd[1:])}\n"
    )
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if f"[mcp_servers.{SERVER_NAME}]" in existing:
        return path, existing  # already configured — don't duplicate
    merged = existing.rstrip("\n") + "\n" + block
    if tomllib is not None:
        tomllib.loads(merged)  # validate: raise on corrupt TOML
    return path, merged


def _claude_target() -> Tuple[Path, Dict[str, Any]]:
    for p in _claude_paths():
        if p.exists():
            return _json_merge(p)
    return _json_merge(_claude_paths()[0])


def _cursor_target() -> Tuple[Path, Dict[str, Any]]:
    return _json_merge(Path.home() / ".cursor" / "mcp.json")


def _gemini_target() -> Tuple[Path, Dict[str, Any]]:
    return _json_merge(Path.home() / ".gemini" / "settings.json")


def _codex_target() -> Tuple[Path, str]:
    return _toml_append(Path.home() / ".codex" / "config.toml")


#: agent_key -> (label, target_fn, detect_fn). Antigravity is manual-only.
AGENTS: Dict[str, Tuple[str, Optional[Callable[[], Tuple[Path, Any]]],
                        Callable[[], bool]]] = {
    "claude": (
        "Claude Code / Claude Desktop",
        _claude_target,
        lambda: any(p.exists() for p in _claude_paths())
        or shutil.which("claude") is not None,
    ),
    "cursor": (
        "Cursor",
        _cursor_target,
        lambda: (Path.home() / ".cursor" / "mcp.json").exists()
        or shutil.which("cursor") is not None,
    ),
    "codex": (
        "Codex CLI / ChatGPT Desktop",
        _codex_target,
        lambda: (Path.home() / ".codex" / "config.toml").exists()
        or shutil.which("codex") is not None,
    ),
    "gemini": (
        "Gemini CLI",
        _gemini_target,
        lambda: (Path.home() / ".gemini" / "settings.json").exists()
        or shutil.which("gemini") is not None,
    ),
    "antigravity": (
        "Antigravity (manual)",
        None,  # no file path — config goes through the UI
        lambda: False,
    ),
}


def detect_agents() -> List[str]:
    """Agent keys with evidence of installation."""
    return [k for k, (_, _, detect) in AGENTS.items() if detect()]


def antigravity_block() -> str:
    """The raw JSON block for Antigravity's manual MCP config UI."""
    cmd = server_command()
    return json.dumps(
        {"mcpServers": {SERVER_NAME: {"command": cmd[0], "args": cmd[1:]}}},
        indent=2,
    )


def plan_agents(keys: List[str]) -> List[Tuple[str, str, Optional[Path], Any]]:
    """Build [(key, label, path_or_None, content)] without writing anything."""
    items = []
    for key in keys:
        if key not in AGENTS:
            raise ValueError(
                f"unknown agent '{key}' — choose from: {', '.join(sorted(AGENTS))}"
            )
        label, target_fn, _ = AGENTS[key]
        if target_fn is None:  # manual-only agent
            items.append((key, label, None, antigravity_block()))
        else:
            path, content = target_fn()
            items.append((key, label, path, content))
    return items


def apply_agents(
    items: List[Tuple[str, str, Optional[Path], Any]], dry_run: bool = False
) -> List[str]:
    """Write planned MCP configs (or print them for --dry-run / manual)."""
    report = []
    for _key, label, path, content in items:
        if path is None:
            # Manual-only agent: show the block, never write.
            report.append(f"{label}: manual step — paste this block:\n{content}")
            continue
        text = json.dumps(content, indent=2) if isinstance(content, dict) else content
        if not text.endswith("\n"):
            text += "\n"
        if dry_run:
            report.append(f"[dry-run] {label}: would write {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        report.append(f"{label}: configured -> {path}")
    return report


# ---------------------------------------------------------------------------
# Channels summary (human 2-minute steps — accounts can't be automated)
# ---------------------------------------------------------------------------

def channels_summary() -> str:
    cmd = server_command()[0]
    lines = [
        "",
        "== Channels — your 2-minute steps (accounts need a human) ==",
        "",
        "  Telegram  — READY NOW",
        "    1. Telegram: message @BotFather -> /newbot -> copy the token",
        "    2. export TELEGRAM_BOT_TOKEN=PASTE_TOKEN_HERE   (or save under",
        '       "telegram": {"bot_token": "..."} in ~/.prana/config.json)',
        "    3. prana-telegram                     # two-way chat on your phone",
        "    Tip: get your chat id from @userinfobot, then schedule",
        '         proactive pings with delivery_target="telegram:<chat_id>"',
        "",
        "  WhatsApp  — READY NOW",
        "    1. developers.facebook.com -> create app -> add WhatsApp product",
        "    2. Note the phone-number ID + a permanent access token",
        "    3. export WHATSAPP_TOKEN=<token> WHATSAPP_PHONE_NUMBER_ID=<id>",
        "    4. prana-whatsapp                       # webhook listener (needs a",
        "       public URL — use a tunnel like cloudflared for local dev)",
        "",
        "  Discord  — READY NOW",
        "    1. discord.com/developers -> New Application -> Bot -> copy token",
        "       (enable the Message Content privileged intent)",
        "    2. OAuth2 -> URL Generator -> scope=bot -> invite to your server",
        "    3. export DISCORD_BOT_TOKEN=<token>",
        "    4. prana-discord                       # two-way chat in your server",
        "",
        "  Voice notes — READY NOW (Telegram)",
        "    1. Same OpenAI key PRANA already uses (whisper + TTS ride on it)",
        "    2. Send a voice note to your bot -> prana-telegram --voice",
        "       (default ON; spoken reply, text fallback if TTS unavailable)",
        "",
        f"  (MCP entries above use python: {cmd} — GUI apps need the full",
        "   path. If you switch interpreters, re-run `prana setup`.)",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --check mode
# ---------------------------------------------------------------------------

def _check_python() -> Tuple[bool, str]:
    ok = sys.version_info >= MIN_PYTHON
    need = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    have = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return ok, f"python {have} (need >={need})"


def _check_config() -> Tuple[bool, str]:
    try:
        d = config.get_config_dir()
    except OSError as e:
        return False, f"config dir unreachable: {e}"
    path = d / config.CONFIG_FILE_NAME
    if not path.exists():
        return False, f"{path} missing — run `prana setup` to create it"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, f"{path} is not a JSON object"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return False, f"{path} unreadable: {e}"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        return False, f"{path} perms {oct(mode)} — expected 0o600"
    return True, f"{path} readable, 0600"


def _check_key() -> Tuple[bool, str]:
    name = key_configured()
    if name:
        return True, f"API key present for {name} (value never shown)"
    return False, "no API key — run `prana setup` or `prana --save-key`"


def _check_mcp_server() -> Tuple[bool, str]:
    if mcp_server_shipped():
        return True, f"{MCP_MODULE} importable"
    return True, (
        f"{MCP_MODULE} not in this build yet — config entries below are "
        "forward-compatible and activate when the server ships"
    )


def _check_agent_entry(key: str) -> Tuple[bool, str]:
    label, target_fn, _ = AGENTS[key]
    if target_fn is None:
        return True, f"{label}: manual (nothing to verify on disk)"
    for p in _claude_paths() if key == "claude" else [plan_agents([key])[0][2]]:
        if p is None or not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            return False, f"{label}: {p} unreadable: {e}"
        if p.suffix == ".toml":
            if tomllib is not None:
                try:
                    data = tomllib.loads(text)
                except Exception as e:
                    return False, f"{label}: {p} invalid TOML: {e}"
                has = (
                    isinstance(data.get("mcp_servers"), dict)
                    and SERVER_NAME in data["mcp_servers"]
                )
            else:  # Python 3.10: fall back to block scan
                has = f"[mcp_servers.{SERVER_NAME}]" in text
            return has, f"{label}: {p} {'has' if has else 'MISSING'} prana block"
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return False, f"{label}: {p} invalid JSON: {e}"
        servers = data.get("mcpServers")
        has = isinstance(servers, dict) and SERVER_NAME in servers
        return has, f"{label}: {p} {'has' if has else 'MISSING'} prana server"
    # No config file on disk: the agent isn't set up here — informational,
    # not a failure. Only existing-but-wrong configs fail the check.
    return True, f"{label}: no config file found — not configured, skipping"


def run_check() -> int:
    """Verify installation health. Returns 0 when everything passes."""
    print("== prana setup --check ==")
    checks: List[Tuple[bool, str]] = [
        _check_python(),
        _check_config(),
        _check_key(),
        _check_mcp_server(),
    ]
    for key in AGENTS:
        checks.append(_check_agent_entry(key))
    failed = 0
    for ok, msg in checks:
        print(f"  [{'OK' if ok else '!!'}] {msg}")
        failed += 0 if ok else 1
    print()
    if failed:
        print(f"{failed} check(s) need attention — see above.")
        return 1
    print("All green — PRANA is ready.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prana-setup",
        description="PRANA instant installer: API key, agent MCP configs, channels.",
    )
    p.add_argument("--key", help="API key (non-interactive); else prompted securely")
    p.add_argument(
        "--provider",
        choices=["openai", "anthropic", "gemini"],
        help="provider for --key (else auto-detected from the key prefix)",
    )
    p.add_argument(
        "--agents",
        help="comma-separated agents (%s) or 'all'; default: auto-detected"
        % ", ".join(sorted(AGENTS)),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="print planned writes, change nothing")
    p.add_argument("--check", action="store_true",
                   help="verify installation health and exit")
    p.add_argument("--skip-key", action="store_true", help="skip the API key step")
    p.add_argument("--skip-agents", action="store_true",
                   help="skip agent MCP configuration")
    p.add_argument("--skip-channels", action="store_true",
                   help="skip the channels summary")
    p.add_argument("--yes", action="store_true",
                   help="non-interactive: accept defaults, no prompts")
    return p


def _confirm(prompt: str, args: argparse.Namespace) -> bool:
    if args.yes or args.dry_run:
        return True
    try:
        ans = input(f"{prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return False
    return ans in ("", "y", "yes")


def _parse_agents(arg: Optional[str]) -> List[str]:
    if not arg:
        detected = detect_agents()
        if detected:
            return detected
        print("no agents detected — configuring all known paths.")
        return [k for k in AGENTS if k != "antigravity"]
    if arg.strip().lower() == "all":
        return list(AGENTS)
    keys = [k.strip().lower() for k in arg.split(",") if k.strip()]
    unknown = [k for k in keys if k not in AGENTS]
    if unknown:
        raise ValueError(
            f"unknown agent(s): {', '.join(unknown)} — choose from: "
            f"{', '.join(sorted(AGENTS))} or 'all'"
        )
    return keys


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.check:
        return run_check()

    print("== prana setup — instant installer ==")
    print(f"python: {sys.executable}")
    if not mcp_server_shipped():
        print(
            f"note: {MCP_MODULE} is not in this build yet — MCP entries are "
            "written forward-compatibly and activate when the server ships."
        )
    print()

    # -- Step 1: API key ----------------------------------------------------
    if not args.skip_key:
        existing = key_configured()
        if args.key:
            try:
                name, path = save_api_key(args.key, args.provider)
            except ProviderError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(f"API key saved for {name} -> {path} (0600)")
        elif existing and not args.yes:
            print(f"API key already configured for {existing} — keeping it.")
            print("  (re-run with --key to replace)")
        elif existing:
            print(f"API key already configured for {existing} — keeping it.")
        else:
            if args.yes:
                print("no --key given and no key saved — skipping key step.",
                      file=sys.stderr)
            else:
                try:
                    raw = prompt_for_key()
                except (EOFError, KeyboardInterrupt):
                    print("cancelled.", file=sys.stderr)
                    return 130
                if not raw:
                    print("empty key — skipped.", file=sys.stderr)
                else:
                    try:
                        name, path = save_api_key(raw, args.provider)
                    except ProviderError as e:
                        print(f"error: {e}", file=sys.stderr)
                        print("hint: pass --provider explicitly if the "
                              "prefix is unusual.", file=sys.stderr)
                        return 2
                    print(f"API key saved for {name} -> {path} (0600)")
        print()

    # -- Step 2: agent MCP configs ------------------------------------------
    if not args.skip_agents:
        try:
            keys = _parse_agents(args.agents)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        labels = ", ".join(AGENTS[k][0] for k in keys)
        print(f"agents: {labels}")
        items = plan_agents(keys)
        if _confirm("Write MCP configs?", args):
            for line in apply_agents(items, dry_run=args.dry_run):
                print(f"  {line}")
        else:
            print("  skipped.")
        print()

    # -- Step 3: channels ----------------------------------------------------
    if not args.skip_channels:
        print(channels_summary())

    print("done. Verify anytime with: python -m prana.setup --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
