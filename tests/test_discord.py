"""Tests for the Discord channel — fully mocked socket + HTTP, zero network.

The gateway is faked with socketpair(): the test drives a scripted "server"
thread (Hello → Identify → READY → dispatches) while DiscordGateway exercises
its real frame encode/decode, handshake, heartbeat, and dispatch paths.
REST calls are faked at the urllib layer like the Telegram tests.
"""

import io
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

from prana.channels.discord import (
    INTENTS,
    INTENT_MESSAGE_CONTENT,
    DiscordClient,
    DiscordError,
    DiscordGateway,
    parse_message_create,
    resolve_discord_token,
    ws_decode_frame,
    ws_encode_text,
)
from prana.channels.discord import MAX_MESSAGE_CHARS
from prana.channels.discord_run import handle_dispatch, run_forever
from prana.channels.run import ConversationStore
from prana.loop import AgentLoop
from prana.providers.base import Completion, Provider
from prana.tools import ToolRegistry


# -- fake gateway server ---------------------------------------------------
class FakeGatewayServer:
    """Scripted stand-in for Discord's gateway, over a socketpair.

    Server → client frames are UNMASKED (like a real server); client frames
    are read with ws_decode_frame (which handles the client's masking).
    """

    def __init__(self):
        self.client_sock, self.server_sock = socket.socketpair()
        self.server_sock.settimeout(5)
        self.identify_msg = None

    def close(self):
        for s in (self.client_sock, self.server_sock):
            try:
                s.close()
            except OSError:
                pass

    def send(self, obj):
        payload = json.dumps(obj).encode("utf-8")
        n = len(payload)
        hdr = bytes([0x81])
        if n < 126:
            hdr += bytes([n])
        elif n < 65536:
            hdr += bytes([126]) + struct.pack(">H", n)
        else:
            hdr += bytes([127]) + struct.pack(">Q", n)
        self.server_sock.sendall(hdr + payload)

    def recv_client(self):
        opcode, payload = ws_decode_frame(self.server_sock)
        assert opcode == 0x1, f"expected text frame, got opcode {opcode}"
        return json.loads(payload.decode("utf-8"))

    def run_handshake(self, token="TESTTOKEN", bot_id="999"):
        """Drive Hello → Identify → READY. Call in a thread (it blocks)."""
        self.send({"op": 10, "d": {"heartbeat_interval": 50}})
        while True:
            msg = self.recv_client()
            if msg.get("op") == 2:  # Identify (heartbeats may come first)
                self.identify_msg = msg
                break
        assert self.identify_msg["d"]["token"] == token
        assert self.identify_msg["d"]["intents"] == INTENTS
        self.send(
            {
                "op": 0,
                "s": 1,
                "t": "READY",
                "d": {"user": {"id": bot_id, "username": "pranabot"}},
            }
        )


def _connect_gateway(monkeypatch, server, token="TESTTOKEN"):
    monkeypatch.setattr(
        DiscordGateway, "_open_socket", lambda self, url: server.client_sock
    )
    gw = DiscordGateway(token, "wss://fake")
    gw.connect()
    return gw


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolate ~/.prana for token-resolution tests."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    return tmp_path


# -- websocket framing -----------------------------------------------------
def test_ws_frame_roundtrip():
    a, b = socket.socketpair()
    try:
        a.sendall(ws_encode_text("hello prana"))
        opcode, payload = ws_decode_frame(b)
        assert opcode == 0x1
        assert payload == b"hello prana"
    finally:
        a.close()
        b.close()


def test_client_frames_are_masked():
    frame = ws_encode_text("hi")
    assert frame[1] & 0x80, "client→server frames must set the MASK bit"


def test_ws_decode_extended_lengths():
    a, b = socket.socketpair()
    try:
        for size in (5000, 70000):  # 16-bit and 64-bit length forms
            text = "x" * size
            payload = text.encode()
            n = len(payload)
            hdr = bytes([0x81])
            if n < 65536:
                hdr += bytes([126]) + struct.pack(">H", n)
            else:
                hdr += bytes([127]) + struct.pack(">Q", n)
            a.sendall(hdr + payload)  # unmasked, like a server
            opcode, out = ws_decode_frame(b)
            assert opcode == 0x1
            assert out.decode() == text
    finally:
        a.close()
        b.close()


def test_ws_decode_close_raises():
    a, b = socket.socketpair()
    try:
        a.sendall(bytes([0x88, 0x00]))  # close frame, empty payload
        with pytest.raises(ConnectionError):
            ws_decode_frame(b)
    finally:
        a.close()
        b.close()


# -- gateway handshake -----------------------------------------------------
def test_gateway_hello_identify_ready(monkeypatch):
    server = FakeGatewayServer()
    t = threading.Thread(target=server.run_handshake, daemon=True)
    t.start()
    gw = _connect_gateway(monkeypatch, server)
    t.join(timeout=5)
    assert not t.is_alive()
    assert gw.bot_user_id == "999"
    assert server.identify_msg["d"]["intents"] == INTENTS
    gw.close()
    server.close()


def test_gateway_rejects_non_hello(monkeypatch):
    server = FakeGatewayServer()

    def script():
        server.send({"op": 99, "d": {}})

    t = threading.Thread(target=script, daemon=True)
    t.start()
    monkeypatch.setattr(
        DiscordGateway, "_open_socket", lambda self, url: server.client_sock
    )
    with pytest.raises(DiscordError, match="Hello"):
        DiscordGateway("T", "wss://fake").connect()
    t.join(timeout=5)
    server.close()


def test_heartbeat_sent_with_current_seq(monkeypatch):
    server = FakeGatewayServer()
    t = threading.Thread(target=server.run_handshake, daemon=True)
    t.start()
    gw = _connect_gateway(monkeypatch, server)
    t.join(timeout=5)
    # Drain client frames until a heartbeat carrying the READY seq (s=1).
    deadline = time.time() + 5
    seen = None
    while time.time() < deadline:
        try:
            msg = server.recv_client()
        except socket.timeout:
            break
        if msg.get("op") == 1 and msg.get("d") == 1:
            seen = msg
            break
    assert seen is not None, "no heartbeat with current sequence observed"
    gw.close()
    server.close()


def test_dispatch_message_create(monkeypatch):
    server = FakeGatewayServer()

    def script():
        server.run_handshake()
        server.send(
            {
                "op": 0,
                "s": 2,
                "t": "MESSAGE_CREATE",
                "d": {
                    "id": "m1",
                    "channel_id": "123",
                    "author": {"id": "42", "username": "prabh"},
                    "content": "hello prana",
                },
            }
        )

    t = threading.Thread(target=script, daemon=True)
    t.start()
    gw = _connect_gateway(monkeypatch, server)
    t.join(timeout=5)
    name, payload = next(gw.iter_dispatches())
    assert name == "MESSAGE_CREATE"
    assert payload["content"] == "hello prana"
    assert payload["channel_id"] == "123"
    gw.close()
    server.close()


def test_reconnect_op_raises(monkeypatch):
    server = FakeGatewayServer()

    def script():
        server.run_handshake()
        server.send({"op": 7, "d": None})

    t = threading.Thread(target=script, daemon=True)
    t.start()
    gw = _connect_gateway(monkeypatch, server)
    t.join(timeout=5)
    with pytest.raises(ConnectionError, match="reconnect"):
        next(gw.iter_dispatches())
    gw.close()
    server.close()


def test_invalid_session_raises_discord_error(monkeypatch):
    server = FakeGatewayServer()

    def script():
        server.run_handshake()
        server.send({"op": 9, "d": False})

    t = threading.Thread(target=script, daemon=True)
    t.start()
    gw = _connect_gateway(monkeypatch, server)
    t.join(timeout=5)
    with pytest.raises(DiscordError, match="invalid session"):
        next(gw.iter_dispatches())
    gw.close()
    server.close()


# -- intents ---------------------------------------------------------------
def test_intents_include_privileged_message_content():
    assert INTENTS == 37377
    assert INTENTS & INTENT_MESSAGE_CONTENT, (
        "MESSAGE_CONTENT intent required — enable it in the portal"
    )


# -- message parsing -------------------------------------------------------
def _mc(channel="123", author="42", content="hi", bot=False):
    return {
        "id": "m1",
        "channel_id": channel,
        "author": {"id": author, "username": "u", "bot": bot},
        "content": content,
    }


def test_parse_message_create_ok():
    assert parse_message_create(_mc(), "999") == ("123", "42", "hi")


def test_parse_message_create_ignores_own():
    assert parse_message_create(_mc(author="999"), "999") is None


def test_parse_message_create_ignores_empty():
    assert parse_message_create(_mc(content="   "), "999") is None
    assert parse_message_create(_mc(content=None), "999") is None


def test_parse_message_create_ignores_malformed():
    assert parse_message_create({}, "999") is None
    assert parse_message_create(None, "999") is None
    assert parse_message_create({"channel_id": "1"}, "999") is None


# -- chunking --------------------------------------------------------------
def test_chunk_limit_is_2000():
    assert MAX_MESSAGE_CHARS == 2000


def test_chunk_long_message_splits_at_2000():
    from prana.channels.telegram import chunk_text

    text = "x" * 5000
    chunks = chunk_text(text, MAX_MESSAGE_CHARS)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


# -- token handling --------------------------------------------------------
def test_missing_token_clean_error(tmp_home):
    with pytest.raises(DiscordError, match="no Discord bot token"):
        resolve_discord_token()


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-secret")
    assert resolve_discord_token() == "discord-secret"


def test_token_never_in_errors(monkeypatch):
    client = DiscordClient("SECRET123")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(DiscordError) as ei:
        client.get_me()
    assert "SECRET123" not in str(ei.value)
    assert "SECRET123" not in repr(client)


def test_main_missing_token_returns_2(tmp_home, capsys):
    from prana.channels import discord_run

    assert discord_run.main([]) == 2
    assert "no Discord bot token" in capsys.readouterr().err


# -- proactive sink registration -------------------------------------------
def test_discord_sink_registered_with_proactivity():
    import prana.channels.discord as dc
    from prana.proactivity import delivery as p3

    dc._maybe_register_with_proactivity()
    assert "discord" in p3._sinks  # our own registry internals


# -- fake REST layer -------------------------------------------------------
class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDiscordHTTP:
    def __init__(self):
        self.requests = []  # (method, api_path, payload)
        self.sent = []  # (channel_id, content)

    def __call__(self, req, timeout=None):
        path = urllib.parse.urlsplit(req.full_url).path
        api_path = path[len("/api/v10") :] if path.startswith("/api/v10") else path
        payload = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append((req.get_method(), api_path, payload))
        if api_path == "/users/@me":
            return FakeHTTPResponse({"id": "999", "username": "pranabot"})
        if api_path == "/gateway/bot":
            return FakeHTTPResponse({"url": "wss://fake"})
        if api_path.startswith("/channels/") and api_path.endswith("/messages"):
            channel_id = api_path.split("/")[2]
            self.sent.append((channel_id, payload["content"]))
            return FakeHTTPResponse({"id": "m9"})
        return FakeHTTPResponse({})


@pytest.fixture
def fake_rest(monkeypatch):
    fake = FakeDiscordHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


# -- end-to-end: fake gateway + fake REST + fake LLM + real loop -----------
class ScriptedProvider(Provider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, script):
        super().__init__(api_key="fake-key")
        self.script = list(script)

    def complete(self, messages, tools, **kwargs):
        return self.script.pop(0) if self.script else Completion(text="done")


def _demo_loop():
    reg = ToolRegistry(approver=lambda n, a: True)
    provider = ScriptedProvider([Completion(text="reply: task complete")])
    return AgentLoop(provider, reg, max_iters=3)


def _run_one_dispatch(monkeypatch, server, client, content, author="42"):
    def script():
        server.run_handshake()
        server.send(
            {
                "op": 0,
                "s": 2,
                "t": "MESSAGE_CREATE",
                "d": {
                    "id": "m1",
                    "channel_id": "123",
                    "author": {"id": author, "username": "u"},
                    "content": content,
                },
            }
        )

    t = threading.Thread(target=script, daemon=True)
    t.start()
    monkeypatch.setattr(
        DiscordGateway, "_open_socket", lambda self, url: server.client_sock
    )
    run_forever(
        client,
        _demo_loop(),
        lambda: DiscordGateway("TESTTOKEN", "wss://fake").connect(),
        max_dispatches=1,
    )
    t.join(timeout=5)


def test_e2e_dispatch_to_reply(monkeypatch, fake_rest, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = FakeGatewayServer()
    try:
        _run_one_dispatch(monkeypatch, server, DiscordClient("TESTTOKEN"), "say hi")
        assert fake_rest.sent[0][1].startswith("\U0001f517 Paired!")  # pairing notice
        assert fake_rest.sent[1] == ("123", "reply: task complete")
    finally:
        server.close()


def test_e2e_help_command_is_local(monkeypatch, fake_rest, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    server = FakeGatewayServer()
    try:
        _run_one_dispatch(monkeypatch, server, DiscordClient("TESTTOKEN"), "!help")
        assert len(fake_rest.sent) == 2
        assert fake_rest.sent[0][1].startswith("\U0001f517 Paired!")  # pairing notice
        assert "PRANA commands" in fake_rest.sent[1][1]
    finally:
        server.close()


def test_e2e_own_message_ignored(monkeypatch, fake_rest):
    server = FakeGatewayServer()
    try:
        _run_one_dispatch(
            monkeypatch, server, DiscordClient("TESTTOKEN"), "echo?", author="999"
        )
        assert fake_rest.sent == []
    finally:
        server.close()


def test_handle_dispatch_ignores_empty_content():
    class FakeClient:
        def __init__(self):
            self.sent = []

        def send_message(self, channel_id, text):
            self.sent.append((channel_id, text))

    store = ConversationStore()
    gw = SimpleNamespace(bot_user_id="999")
    result = handle_dispatch(
        FakeClient(),
        gw,
        _demo_loop(),
        store,
        {"channel_id": "1", "author": {"id": "42"}, "content": "   "},
    )
    assert result is False
