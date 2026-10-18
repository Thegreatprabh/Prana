"""Telegram channel adapter — raw Bot API over stdlib urllib.

No third-party dependencies. Speaks two Bot API methods:
- getUpdates (long polling) for ingress
- sendMessage (with chunking) for egress

The bot token is resolved from the TELEGRAM_BOT_TOKEN env var or
~/.prana/config.json ("telegram": {"bot_token": ...}) and is NEVER logged,
never interpolated into error text, and files holding it are 0600.

Create a bot by talking to @BotFather on Telegram (2 minutes, free).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config as _config
from ..events import Event, user_message

TELEGRAM_API_BASE = "https://api.telegram.org"
#: Telegram caps sendMessage at 4096 chars; we stay under with margin.
MAX_MESSAGE_CHARS = 4000
#: Long-poll window for getUpdates.
POLL_TIMEOUT_S = 30
STATE_FILE_NAME = "telegram.json"
TOKEN_ENV_VAR = "TELEGRAM_BOT_TOKEN"


class TelegramError(Exception):
    """Telegram API / configuration failure. Never contains the bot token."""


def resolve_telegram_token() -> str:
    """Find the bot token: env var first, then config file. Clear error if missing."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    data = _config.load_config()
    tg = data.get("telegram")
    if isinstance(tg, dict):
        token = str(tg.get("bot_token") or "").strip()
        if token:
            return token
    raise TelegramError(
        "no Telegram bot token — set TELEGRAM_BOT_TOKEN, or add "
        '"telegram": {"bot_token": "..."} to ~/.prana/config.json '
        "(create a bot with @BotFather — it takes 2 minutes)"
    )


def _state_path() -> Path:
    return _config.get_config_dir() / STATE_FILE_NAME


def load_offset() -> Optional[int]:
    """Last processed update_id+1, or None on first run. Never raises."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        offset = data.get("offset")
        return int(offset) if offset is not None else None
    except (OSError, ValueError, AttributeError):
        return None


def save_offset(offset: int) -> None:
    """Persist the update offset atomically with 0600 perms. Never raises."""
    try:
        path = _state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> List[str]:
    """Split text into <=limit chunks, preferring paragraph then line breaks.

    Lossless: chunk content is never stripped (trailing spaces can be
    meaningful, e.g. Markdown hard line breaks). Only the blank-line
    *separators* between chunks are dropped, since each chunk is sent as
    its own message.
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else [""]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit  # hard cut as last resort; always makes progress
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c] or [text[:limit]]


#: Characters Telegram's MarkdownV2 requires escaped.
_MARKDOWN_V2_SPECIALS = frozenset("_*[]()~`>#+-=|{}.!")


def escape_markdown_v2(text: str) -> str:
    """Minimal MarkdownV2 escaping for literal text."""
    return "".join(f"\\{ch}" if ch in _MARKDOWN_V2_SPECIALS else ch for ch in text)


def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    try:
        body = err.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if isinstance(data, dict) and data.get("description"):
            return str(data["description"])
        return body[:200]
    except Exception:
        return ""


class TelegramBot:
    """Thin wrapper around the Telegram Bot HTTP API (urllib only)."""

    def __init__(
        self,
        token: str,
        api_base: str = TELEGRAM_API_BASE,
        timeout: int = 60,
    ) -> None:
        if not token or not token.strip():
            raise TelegramError("empty Telegram bot token")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # token never leaks via repr
        return f"TelegramBot(api_base={self._api_base!r}, token=<redacted>)"

    # -- transport ---------------------------------------------------------
    def _post(self, method: str, payload: Dict[str, Any]) -> Any:
        """POST to the Bot API. Error text names the method only — never the URL."""
        url = f"{self._api_base}/bot{self._token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = _read_http_error_body(e)
            raise TelegramError(
                f"Telegram API HTTP {e.code} on {method}: {detail or 'no detail'}"
            )
        except urllib.error.URLError as e:
            raise TelegramError(f"Telegram API unreachable on {method}: {e.reason}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise TelegramError(f"Telegram API bad response on {method}: {e}")
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description", "unknown error") if isinstance(data, dict) else "unknown error"
            raise TelegramError(f"Telegram API error on {method}: {desc}")
        return data.get("result")

    # -- API methods -------------------------------------------------------
    def get_me(self) -> Dict[str, Any]:
        result = self._post("getMe", {})
        return result if isinstance(result, dict) else {}

    def get_updates(
        self, offset: Optional[int] = None, timeout: int = POLL_TIMEOUT_S
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send_message(
        self, chat_id: str, text: str, parse_mode: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Send text, chunked to fit Telegram limits. Returns per-chunk results."""
        results = []
        for chunk in chunk_text(text):
            payload: Dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            results.append(self._post("sendMessage", payload))
        return results

    # -- ingress parsing ---------------------------------------------------
    def parse_update(self, update: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Extract (chat_id, text) from an update, or None if not a text message."""
        if not isinstance(update, dict):
            return None
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        text = msg.get("text", "")
        if chat_id is None or not isinstance(text, str) or not text.strip():
            return None
        return (str(chat_id), text)

    def update_to_event(self, update: Dict[str, Any]) -> Optional[Event]:
        """Convert one Telegram update into a PRANA user_message event."""
        parsed = self.parse_update(update)
        if parsed is None:
            return None
        chat_id, text = parsed
        return user_message(
            text,
            channel="telegram",
            chat_id=chat_id,
            update_id=update.get("update_id"),
        )
