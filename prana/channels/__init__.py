"""Channel adapters — PRANA's voice (SPEC.md §2.6).

A channel is the two-way bridge between the agent and where the user already
lives (Telegram v1; WhatsApp/Discord later). Every adapter follows the same
contract:

    ingress:  poll() -> list[Event]        # inbound user messages become events
    egress:   send(chat_id, text) -> None  # replies AND proactive pings go out

The core loop never knows which channel an event came from — it just sees
`user_message` events with `channel`/`chat_id` in meta. Proactive delivery
(P3) reaches the user through the delivery registry in
`prana.proactivity.delivery`; importing `prana.channels.notify` (done by the
daemon) registers the `telegram` sink there.
"""

from __future__ import annotations

from typing import List, Protocol

from ..events import Event


class ChannelAdapter(Protocol):
    """Contract every channel adapter implements."""

    def send(self, chat_id: str, text: str) -> None:
        """Deliver text to one conversation. Must chunk over-long messages."""
        ...

    def poll(self) -> List[Event]:
        """Return new inbound user_message events since the last poll."""
        ...
