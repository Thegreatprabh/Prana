"""Delivery seam — how outbound text reaches the user.

Targets look like ``"telegram:123456789"`` or ``"console"``. P4 owns the
telegram sink; P3's proactivity delivery registry can register and use these
sinks through :func:`register_sink`.

If ``prana.proactivity.delivery`` exists (P3), the telegram sink is
auto-registered with it on import. The import is guarded: when P3 isn't
there yet, this module works standalone and never crashes.
"""

from __future__ import annotations

from typing import Callable, Dict

#: A sink delivers text to one destination: sink(destination, text).
Sink = Callable[[str, str], None]

_sinks: Dict[str, Sink] = {}


def register_sink(name: str, fn: Sink) -> None:
    """Register (or replace) a named delivery sink."""
    _sinks[name] = fn


def sink_names() -> list:
    return sorted(_sinks)


def _console_sink(_dest: str, text: str) -> None:
    print(text)


def _telegram_sink(chat_id: str, text: str) -> None:
    from .telegram import TelegramBot, resolve_telegram_token

    bot = TelegramBot(resolve_telegram_token())
    bot.send_message(chat_id, text)


def _whatsapp_sink(to_number: str, text: str) -> None:
    from .whatsapp import (
        WhatsAppClient,
        resolve_phone_number_id,
        resolve_whatsapp_token,
    )

    client = WhatsAppClient(
        token=resolve_whatsapp_token(),
        phone_number_id=resolve_phone_number_id(),
    )
    client.send_message(to_number, text)


def _discord_sink(channel_id: str, text: str) -> None:
    from .discord import DiscordClient, resolve_discord_token

    client = DiscordClient(resolve_discord_token())
    client.send_message(channel_id, text)


register_sink("console", _console_sink)
register_sink("telegram", _telegram_sink)
register_sink("whatsapp", _whatsapp_sink)
register_sink("discord", _discord_sink)


def deliver(target: str, text: str) -> None:
    """Send text to ``target`` like ``"telegram:123456789"`` or ``"console"``.

    Raises ValueError for unknown schemes. The telegram sink resolves the bot
    token lazily, so importing this module never requires a token.
    """
    scheme, _, dest = target.partition(":")
    scheme = scheme.strip().lower()
    fn = _sinks.get(scheme)
    if fn is None:
        raise ValueError(
            f"unknown delivery target scheme: {scheme!r} (have: {sink_names()})"
        )
    fn(dest.strip(), text)


def _maybe_register_with_proactivity() -> None:
    """Auto-register channel sinks with P3's delivery registry if present."""
    try:
        from prana.proactivity import delivery as p3_delivery  # type: ignore
    except Exception:
        # P3 absent OR broken/incompatible — channels must import fine either way.
        return
    register = getattr(p3_delivery, "register_sink", None)
    if not callable(register):
        return
    for name, fn in (
        ("telegram", _telegram_sink),
        ("whatsapp", _whatsapp_sink),
        ("discord", _discord_sink),
    ):
        try:
            register(name, fn)
        except Exception:
            pass  # never break import because P3 changed shape


_maybe_register_with_proactivity()
