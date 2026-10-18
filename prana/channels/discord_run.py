"""Discord listener — PRANA on Discord (SPEC.md §2.6).

Gateway ingress (MESSAGE_CREATE dispatches) → user_message Event → AgentLoop
→ reply in the channel. Per-channel conversation history (shared
ConversationStore from run.py) gives the agent continuity without touching
the core loop.

This is also the egress path for proactive pings: importing
``prana.channels.discord`` registers the ``discord`` sink with P3's
delivery registry, so jobs can target ``"discord:<channel_id>"``.

Entry point: ``prana.channels.discord_run:main`` (``prana-discord``
console script).

Discord Developer Portal setup (2 minutes, free):
1. https://discord.com/developers/applications → "New Application" → name it.
2. "Bot" tab → "Reset Token" → copy the token.
3. "Bot" tab → turn ON "Message Content Intent" (privileged intent —
   without it, message text arrives empty and the bot looks dead).
4. "OAuth2" → "URL Generator" → scopes: check "bot" → permissions:
   "Send Messages" + "Read Messages/View Channels" → open the generated
   URL to invite the bot to your server.
5. export DISCORD_BOT_TOKEN=<token>
   (or add "discord": {"bot_token": "..."} to ~/.prana/config.json)
6. prana-discord
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Optional

from .. import config, events
from ..loop import AgentLoop, DEFAULT_MAX_ITERS
from ..personality import MODES, load_identity
from ..providers.base import ProviderError
from ..providers.detect import resolve_provider
from ..tools import ToolRegistry, builtin_tools
from ..tools.memory_tools import builtin_memory_tools
from .discord import (
    DiscordClient,
    DiscordError,
    DiscordGateway,
    parse_message_create,
    resolve_discord_token,
)
from .run import ConversationStore  # shared per-chat history
from . import auth as auth_mod

HELP = (
    "PRANA commands\n"
    "!help — this message\n\n"
    "Anything else is a task or a message for the agent. "
    "Long tasks may take a few tool steps; I'll report when done."
)


def _build_agent(args) -> AgentLoop:
    """Wire provider + tools + loop (mirrors prana/channels/run.py wiring)."""
    key = None
    if args.key:
        key = args.key
    else:
        order = [args.provider] if args.provider else []
        order += [p for p in ("openai", "anthropic", "gemini") if p not in order]
        for prov in order:
            key = config.get_key(prov)
            if key:
                break
    if not key:
        raise ProviderError(
            "no API key found — pass --key, set OPENAI_API_KEY / ANTHROPIC_API_KEY / "
            "GEMINI_API_KEY, or run `python -m prana --save-key`"
        )
    provider_cls = resolve_provider(key, args.provider)
    provider = provider_cls(api_key=key, model=args.model)
    registry = ToolRegistry()
    for tool in builtin_tools(sandbox_root=args.sandbox or os.getcwd()):
        registry.register(tool)
    for tool in builtin_memory_tools():
        registry.register(tool)
    identity = load_identity(args.identity)
    return AgentLoop(
        provider, registry, identity=identity, max_iters=args.max_iters, mode=args.mode
    )


def _is_command(text: str, cmd: str) -> bool:
    first = text.strip().split()[0] if text.strip() else ""
    return first == cmd or first.startswith(cmd + "@")


def handle_dispatch(
    client: DiscordClient,
    gateway: DiscordGateway,
    loop: AgentLoop,
    store: ConversationStore,
    data: dict,
) -> bool:
    """Process one MESSAGE_CREATE payload. Returns True when a reply was sent."""
    parsed = parse_message_create(data, gateway.bot_user_id)
    if parsed is None:
        return False
    channel_id, author_id, text = parsed

    # Pairing gate: only the owner's messages are processed. The first-ever
    # sender becomes the owner; strangers are silently ignored.
    verdict = auth_mod.check_sender("discord", author_id)
    if verdict == "owner":
        client.send_message(channel_id, auth_mod.PAIRING_NOTICE)
    elif verdict == "denied":
        auth_mod.ignore_unpaired("discord")
        return False

    # Local command — no LLM call, instant and token-free.
    if _is_command(text, "!help"):
        client.send_message(channel_id, HELP)
        return True

    context = store.context_block(channel_id)
    prompt = f"{context}\n\n[current message]\n{text}" if context else text
    store.add(channel_id, "user", text)

    event = events.user_message(
        prompt, channel="discord", channel_id=channel_id, author_id=author_id
    )
    result = loop.run(event)
    reply = result.final_text or "(no output)"
    client.send_message(channel_id, reply)
    store.add(channel_id, "assistant", reply)
    return True


def run_forever(
    client: DiscordClient,
    loop: AgentLoop,
    gateway_factory: Callable[[], DiscordGateway],
    *,
    max_dispatches: Optional[int] = None,
) -> None:
    """Listen on the Discord gateway until Ctrl+C.

    A dropped gateway reconnects with backoff (fresh Identify each time).
    max_dispatches bounds the loop for tests/demos.
    """
    backoff = 1.0
    store = ConversationStore()
    processed = 0
    print("🟣 PRANA listening on Discord — Ctrl+C to stop", file=sys.stderr)
    try:
        while True:
            try:
                gw = gateway_factory()
            except (DiscordError, ConnectionError, OSError) as e:
                print(f"discord connect failed: {e} — retrying", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            try:
                with gw:
                    for event_name, payload in gw.iter_dispatches():
                        if event_name != "MESSAGE_CREATE":
                            continue
                        try:
                            handle_dispatch(client, gw, loop, store, payload)
                        except DiscordError as e:
                            print(f"discord send error: {e}", file=sys.stderr)
                        except Exception as e:  # one bad dispatch never kills us
                            print(
                                f"dispatch handling failed: {type(e).__name__}",
                                file=sys.stderr,
                            )
                        processed += 1
                        if (
                            max_dispatches is not None
                            and processed >= max_dispatches
                        ):
                            return
            except (DiscordError, ConnectionError, OSError) as e:
                print(f"discord gateway dropped ({e}) — reconnecting", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    except KeyboardInterrupt:
        print("\nstopping Discord listener", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prana-discord",
        description="PRANA Discord channel — two-way messaging with the agent.",
    )
    p.add_argument("--key", help="LLM API key (else env / ~/.prana/config.json)")
    p.add_argument(
        "--provider",
        choices=["openai", "anthropic", "gemini"],
        help="LLM provider (auto-detected from key if omitted)",
    )
    p.add_argument("--model", help="model name (provider default if omitted)")
    p.add_argument(
        "--max-iters",
        type=int,
        default=DEFAULT_MAX_ITERS,
        help=f"max ReAct iterations per message (default {DEFAULT_MAX_ITERS})",
    )
    p.add_argument("--identity", help="path to SOUL.md-style identity file")
    p.add_argument(
        "--mode",
        choices=sorted(MODES),
        help="personality mode (default from identity file)",
    )
    p.add_argument(
        "--sandbox", help="sandbox root for file/shell tools (default: cwd)"
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = resolve_discord_token()
    except DiscordError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        loop = _build_agent(args)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    client = DiscordClient(token)
    try:
        me = client.get_me()
    except DiscordError as e:
        print(f"error: Discord rejected the bot token: {e}", file=sys.stderr)
        return 2
    try:
        gateway_url = client.get_gateway()
    except DiscordError as e:
        print(f"error: could not get gateway URL: {e}", file=sys.stderr)
        return 2
    print(
        f"🤖 connected as @{me.get('username', '?')} — listening on Discord",
        file=sys.stderr,
    )
    run_forever(client, loop, lambda: DiscordGateway(token, gateway_url).connect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
