"""Telegram poll loop — PRANA's voice (SPEC.md §2.6).

Long-polls getUpdates, turns each message into a user_message Event, runs the
AgentLoop, and sends the reply back. Per-chat conversation history gives the
agent continuity without touching the core loop.

This is also the egress path for proactive pings: P3's delivery registry
sends to ``"telegram:<chat_id>"`` targets (the telegram sink is registered
when ``prana.channels.notify`` is imported — the daemon does this).

Entry point: ``prana.channels.run:main`` (``prana-telegram`` console script).

Live use needs a real bot token from @BotFather (2 minutes, free):
set TELEGRAM_BOT_TOKEN or add "telegram": {"bot_token": "..."} to
~/.prana/config.json.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

from .. import config, events, tokens
from ..loop import AgentLoop, DEFAULT_MAX_ITERS
from ..personality import MODES, load_identity
from ..providers.base import ProviderError
from ..providers.detect import resolve_provider
from ..tools import ToolRegistry, builtin_tools
from ..tools.memory_tools import builtin_memory_tools
from .telegram import (
    POLL_TIMEOUT_S,
    TelegramBot,
    TelegramError,
    load_offset,
    resolve_telegram_token,
    save_offset,
)
from . import voice as voice_mod
from . import auth as auth_mod

INTRO = (
    "🫁 *PRANA* — give any chatbot a life.\n\n"
    "I'm an autonomous agent, not a chatbot. Give me a task and I'll work it "
    "with real tools — shell, files, web — then report back.\n\n"
    "Try: `summarize what you can do` or just tell me what you need.\n"
    "Type /help for commands."
)

HELP = (
    "PRANA commands\n"
    "/start — intro\n"
    "/help — this message\n\n"
    "Anything else is a task or a message for the agent. "
    "Long tasks may take a few tool steps; I'll report when done."
)

#: Rolling history kept per chat for conversational continuity.
HISTORY_PER_CHAT = 40
#: Cap on the injected history block (chars) — token-frugal by design.
HISTORY_CONTEXT_CHARS = 6000


class ConversationStore:
    """Per-chat rolling (role, text) history, newest last."""

    def __init__(self, max_messages: int = HISTORY_PER_CHAT) -> None:
        self._hist: Dict[str, Deque[Tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )

    def add(self, chat_id: str, role: str, text: str) -> None:
        self._hist[str(chat_id)].append((role, text))

    def context_block(self, chat_id: str) -> str:
        items = list(self._hist.get(str(chat_id), []))
        if not items:
            return ""
        lines = ["[recent conversation — for context, newest last]"]
        for role, text in items:
            who = "user" if role == "user" else "assistant"
            lines.append(f"{who}: {tokens.truncate_output(text, 1500)}")
        return tokens.truncate_output("\n".join(lines), HISTORY_CONTEXT_CHARS)


def _build_agent(args) -> AgentLoop:
    """Wire provider + tools + loop (mirrors prana.__main__ wiring)."""
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


def handle_update(
    bot: TelegramBot,
    loop: AgentLoop,
    store: ConversationStore,
    update: dict,
    *,
    voice: Optional[voice_mod.VoiceConfig] = None,
) -> bool:
    """Process one update. Returns True when a reply was sent."""
    # Voice notes first — they don't parse as text messages.
    if voice is not None and voice.enabled and voice_mod.parse_voice_update(update):
        return _handle_voice_update(bot, loop, store, update, voice)

    parsed = bot.parse_update(update)
    if parsed is None:
        return False
    chat_id, text = parsed

    # Pairing gate: only the owner's messages are processed. The first-ever
    # sender becomes the owner; strangers are silently ignored.
    verdict = auth_mod.check_sender("telegram", chat_id)
    if verdict == "owner":
        bot.send_message(chat_id, auth_mod.PAIRING_NOTICE)
    elif verdict == "denied":
        auth_mod.ignore_unpaired("telegram")
        return False

    # Local commands — no LLM call, instant and token-free.
    if _is_command(text, "/start"):
        bot.send_message(chat_id, INTRO)
        return True
    if _is_command(text, "/help"):
        bot.send_message(chat_id, HELP)
        return True

    context = store.context_block(chat_id)
    prompt = f"{context}\n\n[current message]\n{text}" if context else text
    store.add(chat_id, "user", text)

    event = events.user_message(prompt, channel="telegram", chat_id=chat_id)
    result = loop.run(event)
    reply = result.final_text or "(no output)"
    bot.send_message(chat_id, reply)
    store.add(chat_id, "assistant", reply)
    return True


def _handle_voice_update(
    bot: TelegramBot,
    loop: AgentLoop,
    store: ConversationStore,
    update: dict,
    voice_cfg: voice_mod.VoiceConfig,
) -> bool:
    """Voice note → transcribe → agent thinks → voice reply (or text fallback)."""
    parsed = voice_mod.parse_voice_update(update)
    if parsed is None:
        return False
    chat_id, _file_id = parsed

    # Pairing gate (same as text path).
    verdict = auth_mod.check_sender("telegram", chat_id)
    if verdict == "owner":
        bot.send_message(chat_id, auth_mod.PAIRING_NOTICE)
    elif verdict == "denied":
        auth_mod.ignore_unpaired("telegram")
        return False

    text = voice_mod.handle_voice_message(
        bot, update, openai_key=voice_cfg.openai_key
    )
    if not text:
        bot.send_message(chat_id, voice_mod.TRANSCRIBE_FALLBACK)
        return True

    context = store.context_block(chat_id)
    prompt = f"{context}\n\n[current message — voice note]\n{text}" if context else text
    store.add(chat_id, "user", text)

    event = events.user_message(
        prompt, channel="telegram", chat_id=chat_id, via="voice"
    )
    result = loop.run(event)
    reply = result.final_text or "(no output)"

    sent_as_voice = False
    if voice_cfg.openai_key:
        try:
            audio = voice_mod.speak(reply, voice_cfg.openai_key)
            voice_mod.send_voice_note(bot, chat_id, audio)
            sent_as_voice = True
        except Exception as e:
            # Any TTS failure -> text fallback. The agent already did the
            # work; the user must still get the reply.
            print(f"voice reply failed, falling back to text: {e}", file=sys.stderr)
    if not sent_as_voice:
        bot.send_message(chat_id, reply)
    store.add(chat_id, "assistant", reply)
    return True


def run_forever(
    bot: TelegramBot,
    loop: AgentLoop,
    *,
    poll_timeout: int = POLL_TIMEOUT_S,
    max_polls: Optional[int] = None,
    voice: Optional[voice_mod.VoiceConfig] = None,
) -> None:
    """Poll Telegram until Ctrl+C. max_polls bounds the loop for tests/demos."""
    offset = load_offset()
    store = ConversationStore()
    polls = 0
    print("📲 PRANA listening on Telegram — Ctrl+C to stop", file=sys.stderr)
    try:
        while True:
            if max_polls is not None and polls >= max_polls:
                break
            polls += 1
            try:
                updates = bot.get_updates(offset=offset, timeout=poll_timeout)
            except TelegramError as e:
                print(f"telegram poll error: {e} — retrying in 5s", file=sys.stderr)
                time.sleep(5)
                continue
            for update in updates:
                try:
                    update_id = int(update.get("update_id", 0))
                except (TypeError, ValueError):
                    continue
                offset = max(offset or 0, update_id + 1)
                try:
                    handle_update(bot, loop, store, update, voice=voice)
                except TelegramError as e:
                    print(f"telegram send error: {e}", file=sys.stderr)
                except Exception as e:  # one bad update never kills the listener
                    print(f"update handling failed: {type(e).__name__}", file=sys.stderr)
            if offset is not None:
                save_offset(offset)
    except KeyboardInterrupt:
        print("\nstopping Telegram listener", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prana-telegram",
        description="PRANA Telegram channel — two-way messaging with the agent.",
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
    p.add_argument(
        "--poll-timeout",
        type=int,
        default=POLL_TIMEOUT_S,
        help=f"getUpdates long-poll seconds (default {POLL_TIMEOUT_S})",
    )
    p.add_argument(
        "--voice",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="voice notes in/out via Telegram (default: on; needs an OpenAI key, "
        "else graceful text fallback)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = resolve_telegram_token()
    except TelegramError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        loop = _build_agent(args)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    bot = TelegramBot(token)
    try:
        me = bot.get_me()
    except TelegramError as e:
        print(f"error: bot token rejected by Telegram: {e}", file=sys.stderr)
        return 2
    print(
        f"🤖 connected as @{me.get('username', '?')} "
        f"({me.get('first_name', '?')}) — polling",
        file=sys.stderr,
    )
    voice_cfg = voice_mod.VoiceConfig(
        enabled=args.voice,
        openai_key=voice_mod.resolve_voice_key(args.key, args.provider)
        if args.voice
        else None,
    )
    if args.voice and not voice_cfg.openai_key:
        print(
            "voice: no OpenAI key — voice notes will transcribe/reply as text "
            "(run `prana --save-key` with an OpenAI key for full voice)",
            file=sys.stderr,
        )
    run_forever(bot, loop, poll_timeout=args.poll_timeout, voice=voice_cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
