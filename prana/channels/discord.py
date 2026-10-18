"""Discord channel adapter — REST + Gateway over stdlib only.

No third-party dependencies (no discord.py): the small pieces of the
Discord API we need are implemented by hand.

Ingress: Discord Gateway (websocket, v10, JSON encoding). We connect,
answer the Hello with heartbeats, Identify, wait for READY (which tells us
our own user id so we can ignore our own messages), then yield
MESSAGE_CREATE dispatches.

Egress: POST /channels/<id>/messages, chunked at 2000 chars.

Protocol notes (Discord API v10):
- Gateway URL comes from GET /gateway/bot → {"url": "wss://..."}.
- Op 10 Hello → {"d": {"heartbeat_interval": ms}}.
- Op 1 Heartbeat → {"d": <last sequence or null>}, on the Hello interval.
- Op 2 Identify → {"d": {"token", "intents", "properties"}}.
- Op 0 Dispatch → {"t": event name, "s": seq, "d": payload}.
- Op 7 Reconnect → drop and reconnect. Op 9 Invalid Session → re-identify.
- Op 11 Heartbeat ACK → nothing to do.
- WebSocket frames (RFC 6455): client→server frames MUST be masked.

Intents needed for MESSAGE_CREATE with readable text:
- GUILDS (1<<0), GUILD_MESSAGES (1<<9), DIRECT_MESSAGES (1<<12),
- MESSAGE_CONTENT (1<<15) — PRIVILEGED: toggle "Message Content Intent"
  in the Discord developer portal (Bot tab), or message content arrives empty.

The bot token is resolved from the DISCORD_BOT_TOKEN env var or
~/.prana/config.json ("discord": {"bot_token": ...}) and is NEVER logged,
never interpolated into error text, and files holding it are 0600.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import config as _config
from .telegram import chunk_text  # shared lossless chunking rule

DISCORD_API_BASE = "https://discord.com/api/v10"
GATEWAY_VERSION = 10
#: Discord caps a message at 2000 chars; we stay strictly under.
MAX_MESSAGE_CHARS = 2000
TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"

# -- gateway intents -------------------------------------------------------
INTENT_GUILDS = 1 << 0
INTENT_GUILD_MESSAGES = 1 << 9
INTENT_DIRECT_MESSAGES = 1 << 12
#: PRIVILEGED — must be enabled in the developer portal (Bot → Message Content Intent).
INTENT_MESSAGE_CONTENT = 1 << 15
INTENTS = (
    INTENT_GUILDS
    | INTENT_GUILD_MESSAGES
    | INTENT_DIRECT_MESSAGES
    | INTENT_MESSAGE_CONTENT
)  # == 37377

# -- gateway opcodes -------------------------------------------------------
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

_WS_CONT = 0x0
_WS_TEXT = 0x1
_WS_CLOSE = 0x8
_WS_PING = 0x9
_WS_PONG = 0xA


class DiscordError(Exception):
    """Discord API / gateway / configuration failure. Never contains the token."""


def resolve_discord_token() -> str:
    """Find the bot token: env var first, then config file. Clear error if missing."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    data = _config.load_config()
    dc = data.get("discord")
    if isinstance(dc, dict):
        token = str(dc.get("bot_token") or "").strip()
        if token:
            return token
    raise DiscordError(
        "no Discord bot token — set DISCORD_BOT_TOKEN, or add "
        '"discord": {"bot_token": "..."} to ~/.prana/config.json '
        "(see prana/channels/discord_run.py for the 2-minute portal setup)"
    )


# -- websocket framing (RFC 6455) ------------------------------------------
def _ws_mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def ws_encode_text(text: str) -> bytes:
    """Encode a client→server masked text frame."""
    payload = text.encode("utf-8")
    key = os.urandom(4)
    n = len(payload)
    header = bytearray([0x80 | _WS_TEXT])  # FIN + text opcode
    if n < 126:
        header.append(0x80 | n)  # MASK bit set — clients must mask
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    return bytes(header) + key + _ws_mask(payload, key)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("gateway connection closed")
        buf += chunk
    return bytes(buf)


def ws_decode_frame(sock: socket.socket) -> Tuple[int, bytes]:
    """Read one server→client frame. Returns (opcode, payload).

    Raises ConnectionError on a close frame or EOF — the caller reconnects.
    """
    hdr = _recv_exact(sock, 2)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack(">Q", _recv_exact(sock, 8))
    if masked:
        key = _recv_exact(sock, 4)
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = _ws_mask(payload, key)
    if opcode == _WS_CLOSE:
        raise ConnectionError("gateway sent close frame")
    return opcode, payload


# -- REST client -----------------------------------------------------------
def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    try:
        body = err.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])[:200]
        return body[:200]
    except Exception:
        return ""


class DiscordClient:
    """Thin wrapper around the Discord REST API (urllib only)."""

    def __init__(
        self,
        token: str,
        api_base: str = DISCORD_API_BASE,
        timeout: int = 60,
    ) -> None:
        if not token or not token.strip():
            raise DiscordError("empty Discord bot token")
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # token never leaks via repr
        return f"DiscordClient(api_base={self._api_base!r}, token=<redacted>)"

    def _request(
        self, method: str, path: str, payload: Optional[Dict[str, Any]] = None
    ) -> Any:
        """HTTP call. Error text names the method/path only — never the token."""
        url = f"{self._api_base}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                # NOTE: the token goes in the header, never in the URL.
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "prana-discord/0.1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = _read_http_error_body(e)
            raise DiscordError(
                f"Discord API HTTP {e.code} on {method} {path}: {detail or 'no detail'}"
            )
        except urllib.error.URLError as e:
            raise DiscordError(f"Discord API unreachable on {method} {path}: {e.reason}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise DiscordError(f"Discord API bad response on {method} {path}: {e}")
        if isinstance(data, dict) and data.get("code"):
            # Discord error envelope: {"code": 50001, "message": "..."}
            raise DiscordError(
                f"Discord API error on {method} {path}: {data.get('message', 'unknown')}"
            )
        return data

    def get_me(self) -> Dict[str, Any]:
        """Validate the token; returns the bot's user object."""
        result = self._request("GET", "/users/@me")
        return result if isinstance(result, dict) else {}

    def get_gateway(self) -> str:
        """WSS gateway URL for this bot."""
        result = self._request("GET", "/gateway/bot")
        if isinstance(result, dict) and result.get("url"):
            return str(result["url"])
        raise DiscordError("Discord API: /gateway/bot returned no url")

    def send_message(self, channel_id: str, text: str) -> List[Dict[str, Any]]:
        """Send text, chunked to fit Discord's 2000-char limit."""
        results = []
        for chunk in chunk_text(text, MAX_MESSAGE_CHARS):
            results.append(
                self._request(
                    "POST",
                    f"/channels/{channel_id}/messages",
                    {"content": chunk},
                )
            )
        return results


# -- ingress parsing -------------------------------------------------------
def parse_message_create(
    data: Dict[str, Any], bot_user_id: Optional[str]
) -> Optional[Tuple[str, str, str]]:
    """Extract (channel_id, author_id, text) from a MESSAGE_CREATE payload.

    Returns None for anything that isn't a real user message: malformed
    payloads, our own bot's messages, and empty content.
    """
    if not isinstance(data, dict):
        return None
    channel_id = data.get("channel_id")
    author = data.get("author") or {}
    author_id = author.get("id")
    if not channel_id or not author_id:
        return None
    if bot_user_id and str(author_id) == str(bot_user_id):
        return None  # our own message — never reply to ourselves
    text = data.get("content") or ""
    if not isinstance(text, str) or not text.strip():
        return None
    return str(channel_id), str(author_id), text


# -- gateway client --------------------------------------------------------
class DiscordGateway:
    """Minimal Discord Gateway client (v10, JSON encoding), stdlib only.

    Usage:
        gw = DiscordGateway(token, gateway_url).connect()
        with gw:
            for event_name, payload in gw.iter_dispatches():
                ...

    Reconnects are NOT resumed — a dropped connection re-identifies fresh
    (documented simplification; at-most-once delivery per session).
    """

    def __init__(
        self,
        token: str,
        gateway_url: str,
        intents: int = INTENTS,
        timeout: int = 60,
    ) -> None:
        if not token or not token.strip():
            raise DiscordError("empty Discord bot token")
        self._token = token
        self._gateway_url = gateway_url
        self._intents = intents
        self._timeout = timeout
        self.bot_user_id: Optional[str] = None
        self._sock: Optional[socket.socket] = None
        self._seq: Optional[int] = None
        self._seq_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._heartbeat_interval_s = 40.0

    def __repr__(self) -> str:  # token never leaks via repr
        return f"DiscordGateway(url={self._gateway_url!r}, token=<redacted>)"

    # -- connection ------------------------------------------------------
    def _open_socket(self, url: str) -> socket.socket:
        """TCP + TLS + WebSocket handshake. Monkeypatched in tests."""
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "gateway.discord.gg"
        port = parts.port or 443
        path = parts.path or "/"
        raw = socket.create_connection((host, port), timeout=self._timeout)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path}?v={GATEWAY_VERSION}&encoding=json HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(req.encode("ascii"))
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("gateway handshake: no response")
            resp += chunk
            if len(resp) > 16384:
                raise ConnectionError("gateway handshake: header too large")
        status = resp.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError(f"gateway handshake rejected: {status[:80]!r}")
        return sock

    def _send(self, obj: Dict[str, Any]) -> None:
        data = ws_encode_text(json.dumps(obj))
        with self._send_lock:
            if self._sock is None:
                raise ConnectionError("gateway not connected")
            self._sock.sendall(data)

    def _set_seq(self, seq: Optional[int]) -> None:
        if seq is not None:
            with self._seq_lock:
                self._seq = seq

    def _get_seq(self) -> Optional[int]:
        with self._seq_lock:
            return self._seq

    def _heartbeat_loop(self) -> None:
        while True:
            try:
                self._send({"op": OP_HEARTBEAT, "d": self._get_seq()})
            except OSError:
                break
            if self._stop.wait(self._heartbeat_interval_s):
                break

    def connect(self) -> "DiscordGateway":
        """Handshake → Hello → heartbeat → Identify → wait for READY."""
        self._sock = self._open_socket(self._gateway_url)
        opcode, payload = ws_decode_frame(self._sock)
        if opcode != _WS_TEXT:
            raise DiscordError("gateway: expected text frame for Hello")
        try:
            hello = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise DiscordError(f"gateway: bad Hello frame: {e}")
        if hello.get("op") != OP_HELLO or not isinstance(hello.get("d"), dict):
            raise DiscordError("gateway: expected Op 10 Hello")
        interval_ms = hello["d"].get("heartbeat_interval", 40000)
        try:
            self._heartbeat_interval_s = float(interval_ms) / 1000.0
        except (TypeError, ValueError):
            self._heartbeat_interval_s = 40.0

        self._stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="prana-discord-hb"
        )
        self._hb_thread.start()

        # Identify (token travels over TLS only; never logged).
        self._send(
            {
                "op": OP_IDENTIFY,
                "d": {
                    "token": self._token,
                    "intents": self._intents,
                    "properties": {
                        "os": "linux",
                        "browser": "prana",
                        "device": "prana",
                    },
                },
            }
        )

        # Wait for READY to learn our own user id (bounded — never hangs).
        deadline = time.time() + 30
        while time.time() < deadline:
            opcode, payload = ws_decode_frame(self._sock)
            if opcode != _WS_TEXT:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if msg.get("op") != OP_DISPATCH:
                continue
            self._set_seq(msg.get("s"))
            if msg.get("t") == "READY":
                user = (msg.get("d") or {}).get("user") or {}
                if user.get("id"):
                    self.bot_user_id = str(user["id"])
                return self
        raise DiscordError("gateway: timed out waiting for READY")

    # -- dispatch iteration ------------------------------------------------
    def _iter_raw(self) -> Iterator[Tuple[Optional[str], Dict[str, Any]]]:
        """Yield (event_name, data) for dispatches; handle protocol frames."""
        assert self._sock is not None
        while True:
            opcode, payload = ws_decode_frame(self._sock)
            if opcode == _WS_PING:
                with self._send_lock:
                    self._sock.sendall(bytes([0x80 | _WS_PONG, 0x00]))
                continue
            if opcode != _WS_TEXT:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ConnectionError("gateway: bad frame payload")
            op = msg.get("op")
            if op == OP_DISPATCH:
                self._set_seq(msg.get("s"))
                yield msg.get("t"), (msg.get("d") or {})
            elif op == OP_HEARTBEAT_ACK:
                continue
            elif op == OP_RECONNECT:
                raise ConnectionError("gateway requested reconnect")
            elif op == OP_INVALID_SESSION:
                raise DiscordError("gateway: invalid session (bad token?)")
            # Other ops are informational — ignore.

    def iter_dispatches(self) -> Iterator[Tuple[str, Dict[str, Any]]]:
        """Yield (event_name, payload) until the connection drops."""
        for t, d in self._iter_raw():
            if t:
                yield t, d

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5)
            self._hb_thread = None

    def __enter__(self) -> "DiscordGateway":
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False


# -- proactive delivery sink -------------------------------------------------
def _discord_sink(channel_id: str, text: str) -> None:
    """Deliver a proactive ping to a Discord channel. Registered below."""
    client = DiscordClient(resolve_discord_token())
    client.send_message(channel_id, text)


def _maybe_register_with_proactivity() -> None:
    """Auto-register the discord sink with P3's delivery registry if present.

    Mirrors prana/channels/notify.py. The daemon imports this module to
    enable ``"discord:<channel_id>"`` delivery targets.
    """
    try:
        from ..proactivity import delivery as p3_delivery  # type: ignore
    except Exception:
        # P3 absent OR broken/incompatible — discord must import fine either way.
        return
    register = getattr(p3_delivery, "register_sink", None)
    if callable(register):
        try:
            register("discord", _discord_sink)
        except Exception:
            pass  # never break import because P3 changed shape


_maybe_register_with_proactivity()
