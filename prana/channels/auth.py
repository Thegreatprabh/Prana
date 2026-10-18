"""Channel pairing — only the owner's messages are answered.

Threat model: a Telegram bot username can be guessed, a Discord bot can be
DM'd by anyone in a shared server, a WhatsApp number can be messaged by
anyone. Without pairing, a stranger gets a full ReAct agent with shell
tools on the host. So:

- The FIRST-EVER sender on a channel becomes the **owner** (pairing).
- Everyone else is silently ignored — no reply, no error, nothing to probe.
- The allowlist lives in ``~/.prana/config.json`` under
  ``["channels"][<channel>]["allowed_ids"]`` (0600, same as API keys).

``check_sender(channel, sender_id)`` returns ``"owner"`` (just paired),
``"allowed"`` (already paired), or ``"denied"``.
"""

from __future__ import annotations

import sys

from .. import config as _config

PAIRING_NOTICE = (
    "\U0001f517 Paired! You are now the owner of this PRANA instance. "
    "Only your messages will be answered here."
)


def check_sender(channel: str, sender_id: str) -> str:
    """Pair-or-check a sender. Never raises (fail closed → denied)."""
    try:
        sid = str(sender_id)
        allowed = _config.get_channel_allowed_ids(channel)
        if not allowed:
            _config.add_channel_allowed_id(channel, sid)
            return "owner"
        return "allowed" if sid in allowed else "denied"
    except Exception:
        return "denied"


def ignore_unpaired(channel: str) -> None:
    """Log an unpaired-sender event WITHOUT the sender id (PII-safe)."""
    print(f"unpaired sender ignored on {channel}", file=sys.stderr)


__all__ = ["PAIRING_NOTICE", "check_sender", "ignore_unpaired"]
