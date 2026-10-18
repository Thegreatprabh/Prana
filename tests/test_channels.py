"""Tests for the Telegram channel — fully mocked HTTP, zero network.

urlopen is faked at the urllib layer so TelegramBot exercises its real
request/response/error paths against canned payloads.
"""

import io
import json
import urllib.error
import urllib.request

import pytest

from prana import events
from prana.channels import notify
from prana.channels.telegram import (
    TelegramBot,
    TelegramError,
    chunk_text,
    escape_markdown_v2,
    load_offset,
    resolve_telegram_token,
    save_offset,
)
from prana.loop import AgentLoop
from prana.providers.base import Completion, Provider
from prana.tools import ToolRegistry


# -- fake HTTP layer -------------------------------------------------------
class FakeHTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTelegramHTTP:
    """Scripted stand-in for Telegram's servers.

    queued_updates: list of update dicts served across getUpdates calls.
    records every request (method, payload); sent messages are inspectable.
    """

    def __init__(self, queued_updates=None, fail_send=False):
        self.queued = list(queued_updates or [])
        self.requests = []  # (method, payload)
        self.sent = []  # (chat_id, text) from sendMessage
        self.fail_send = fail_send

    def __call__(self, req, timeout=None):
        url = req.full_url
        method = url.rsplit("/", 1)[-1]
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.requests.append((method, payload))
        if method == "getMe":
            return FakeHTTPResponse(
                {"ok": True, "result": {"username": "prana_test_bot", "first_name": "PRANA"}}
            )
        if method == "getUpdates":
            offset = payload.get("offset")
            batch = [
                u
                for u in self.queued
                if offset is None or u.get("update_id", 0) >= offset
            ]
            self.queued = [
                u
                for u in self.queued
                if not (offset is None or u.get("update_id", 0) >= offset)
            ]
            return FakeHTTPResponse({"ok": True, "result": batch})
        if method == "sendMessage":
            if self.fail_send:
                raise urllib.error.HTTPError(
                    url, 400, "Bad Request", {}, io.BytesIO(b'{"ok": false}')
                )
            self.sent.append((payload["chat_id"], payload["text"]))
            return FakeHTTPResponse({"ok": True, "result": {"message_id": 1}})
        return FakeHTTPResponse({"ok": True, "result": True})


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeTelegramHTTP()

    def _urlopen(req, timeout=None):
        return fake(req, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    return fake


@pytest.fixture
def bot():
    return TelegramBot("TESTTOKEN")


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Isolate ~/.prana for state/config tests."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return tmp_path


def _update(uid, chat_id, text):
    return {
        "update_id": uid,
        "message": {"message_id": uid, "chat": {"id": chat_id}, "text": text},
    }


# -- parsing ---------------------------------------------------------------
def test_parse_text_update(bot):
    assert bot.parse_update(_update(1, 42, "hello")) == ("42", "hello")


def test_parse_start_command(bot):
    assert bot.parse_update(_update(1, 42, "/start")) == ("42", "/start")


def test_parse_ignores_non_message(bot):
    assert bot.parse_update({"update_id": 1}) is None  # no message key
    assert bot.parse_update(_update(1, 42, "   ")) is None  # blank text
    assert (
        bot.parse_update(
            {"update_id": 2, "message": {"chat": {"id": 7}}}  # sticker, no text
        )
        is None
    )


def test_update_to_event(bot):
    ev = bot.update_to_event(_update(9, 42, "do the thing"))
    assert isinstance(ev, events.Event)
    assert ev.kind == "user_message"
    assert ev.text == "do the thing"
    assert ev.meta["channel"] == "telegram"
    assert ev.meta["chat_id"] == "42"


# -- chunking --------------------------------------------------------------
def test_chunk_short_unchanged():
    assert chunk_text("hi") == ["hi"]


def test_chunk_prefers_paragraph_boundary():
    text = "a" * 3990 + "\n\n" + "b" * 100
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0] == "a" * 3990
    assert chunks[1] == "b" * 100


def test_chunk_long_message_splits(fake_http, bot):
    long_text = "x" * 5000
    bot.send_message("42", long_text)
    sends = [r for r in fake_http.requests if r[0] == "sendMessage"]
    assert len(sends) == 2
    assert all(len(p["text"]) <= 4000 for _, p in sends)
    assert "".join(p["text"] for _, p in sends) == long_text


def test_escape_markdown_v2():
    assert escape_markdown_v2("a_b*c[d]") == "a\\_b\\*c\\[d\\]"


# -- token handling --------------------------------------------------------
def test_missing_token_clean_error(tmp_home):
    with pytest.raises(TelegramError, match="no Telegram bot token"):
        resolve_telegram_token()


def test_token_from_env(tmp_home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token-123")
    assert resolve_telegram_token() == "env-token-123"


def test_token_never_in_errors(fake_http):
    secret = "SECRET123"
    bot = TelegramBot(secret)

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}")
        )

    import urllib.request as _u

    orig = _u.urlopen
    _u.urlopen = boom
    try:
        with pytest.raises(TelegramError) as ei:
            bot.get_me()
    finally:
        _u.urlopen = orig
    assert secret not in str(ei.value)
    assert "SECRET123" not in repr(bot)


# -- offset persistence ----------------------------------------------------
def test_offset_roundtrip(tmp_home, monkeypatch):
    import prana.channels.telegram as tg

    monkeypatch.setattr(tg, "_state_path", lambda: tmp_home / "telegram.json")
    assert tg.load_offset() is None
    tg.save_offset(123)
    assert tg.load_offset() == 123
    import os, stat

    assert stat.S_IMODE(os.stat(tmp_home / "telegram.json").st_mode) == 0o600


# -- notify routing --------------------------------------------------------
def test_notify_console(capsys):
    notify.deliver("console", "hello console")
    assert "hello console" in capsys.readouterr().out


def test_notify_unknown_scheme():
    with pytest.raises(ValueError, match="unknown delivery target scheme"):
        notify.deliver("pigeon:42", "coo")


def test_notify_telegram_uses_sink(monkeypatch):
    calls = []
    notify.register_sink("telegram", lambda dest, text: calls.append((dest, text)))
    notify.deliver("telegram:777", "ping")
    assert calls == [("777", "ping")]


# -- end-to-end: fake Telegram server + fake LLM + real loop ---------------
class ScriptedProvider(Provider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, script):
        super().__init__(api_key="fake-key")
        self.script = list(script)

    def complete(self, messages, tools, **kwargs):
        return self.script.pop(0) if self.script else Completion(text="done")


def _demo_loop(tmp_path):
    from prana.tools import builtin_file_tools, builtin_shell_tools

    reg = ToolRegistry(approver=lambda n, a: True)
    for t in builtin_file_tools(str(tmp_path)):
        reg.register(t)
    for t in builtin_shell_tools(str(tmp_path)):
        reg.register(t)
    provider = ScriptedProvider([Completion(text="reply: task complete")])
    return AgentLoop(provider, reg, max_iters=3)


def test_e2e_message_in_reply_out(fake_http, tmp_path, monkeypatch):
    """Fake Telegram server → message in → real loop → reply out."""
    import prana.channels.telegram as tg

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tg, "_state_path", lambda: tmp_path / "telegram.json")
    fake_http.queued.append(_update(10, "99", "say hi"))

    from prana.channels import run as run_mod

    bot = TelegramBot("TESTTOKEN")
    loop = _demo_loop(tmp_path)
    run_mod.run_forever(bot, loop, poll_timeout=1, max_polls=2)

    assert fake_http.sent[0][1].startswith("\U0001f517 Paired!")  # pairing notice first
    assert fake_http.sent[1] == ("99", "reply: task complete")
    assert tg.load_offset() == 11  # update_id 10 consumed → offset 11


def test_e2e_start_command_gets_intro(fake_http, tmp_path, monkeypatch):
    import prana.channels.telegram as tg

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tg, "_state_path", lambda: tmp_path / "telegram.json")
    fake_http.queued.append(_update(11, "99", "/start"))

    from prana.channels import run as run_mod

    bot = TelegramBot("TESTTOKEN")
    loop = _demo_loop(tmp_path)
    run_mod.run_forever(bot, loop, poll_timeout=1, max_polls=2)

    assert len(fake_http.sent) == 2
    assert fake_http.sent[0][1].startswith("\U0001f517 Paired!")  # pairing notice
    assert "PRANA" in fake_http.sent[1][1]


def test_e2e_send_failure_does_not_kill_listener(fake_http, tmp_path, monkeypatch):
    import prana.channels.telegram as tg

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(tg, "_state_path", lambda: tmp_path / "telegram.json")
    fake_http.queued.append(_update(12, "99", "say hi"))
    fake_http.fail_send = True

    from prana.channels import run as run_mod

    bot = TelegramBot("TESTTOKEN")
    loop = _demo_loop(tmp_path)
    # Must not raise: the listener survives a failed send.
    run_mod.run_forever(bot, loop, poll_timeout=1, max_polls=2)
    assert fake_http.sent == []
