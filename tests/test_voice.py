"""Tests for Telegram voice notes — fully mocked HTTP, zero network.

urlopen is faked so the voice pipeline exercises its real request/response/
error paths against canned payloads. No Telegram or OpenAI calls are made.
"""

import io
import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from prana.channels import voice as voice_mod
from prana.channels.run import ConversationStore, build_parser, handle_update
from prana.channels.voice import (
    TRANSCRIBE_FALLBACK,
    VoiceConfig,
    VoiceError,
    _encode_multipart,
    download_voice,
    handle_voice_message,
    parse_voice_update,
    resolve_voice_key,
    send_voice_note,
    speak,
    transcribe_ogg,
)


# -- fake HTTP layer -------------------------------------------------------
class FakeHTTPResponse:
    def __init__(self, payload_bytes, content_type="application/json"):
        self._payload = payload_bytes

    def read(self, amt=None):
        return self._payload if amt is None else self._payload[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeURLopen:
    """Route-based fake for urllib.request.urlopen. Records every request."""

    def __init__(self):
        self.requests = []  # (url, headers dict, body bytes)
        self.routes = []  # (predicate(url, body), responder) in order

    def add(self, predicate, responder):
        self.routes.append((predicate, responder))

    def __call__(self, req, timeout=None):
        url = req.full_url
        body = req.data or b""
        headers = dict(req.header_items())
        self.requests.append((url, headers, body))
        for predicate, responder in self.routes:
            if predicate(url, body):
                result = responder(url, body)
                if isinstance(result, Exception):
                    raise result
                if isinstance(result, bytes):
                    return FakeHTTPResponse(result)
                return FakeHTTPResponse(json.dumps(result).encode("utf-8"))
        raise AssertionError(f"no fake route for {url}")


@pytest.fixture
def fake_http(monkeypatch):
    fh = FakeURLopen()
    monkeypatch.setattr(urllib.request, "urlopen", fh)
    return fh


@pytest.fixture
def no_openai_key(monkeypatch):
    monkeypatch.setattr(voice_mod._config, "get_key", lambda provider: None)


class FakeBot:
    """Minimal stand-in: only what the voice pipeline touches."""

    def __init__(self, getfile_result=None):
        self.getfile_result = getfile_result
        self.post_calls = []

    def _post(self, method, payload):
        self.post_calls.append((method, payload))
        if method == "getFile":
            return self.getfile_result
        raise AssertionError(f"unexpected _post {method}")


class FakeLoop:
    def __init__(self, reply="FINAL: done"):
        self.reply = reply
        self.seen_events = []

    def run(self, event):
        self.seen_events.append(event)
        return SimpleNamespace(final_text=self.reply)


class FakeSendBot:
    """Records send_message calls (text path)."""

    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((str(chat_id), text))
        return []

    def parse_update(self, update):
        # Mirrors TelegramBot.parse_update for text messages.
        msg = (update or {}).get("message") or {}
        text = msg.get("text", "")
        chat = msg.get("chat") or {}
        if chat.get("id") is None or not isinstance(text, str) or not text.strip():
            return None
        return (str(chat["id"]), text)


VOICE_UPDATE = {
    "update_id": 7,
    "message": {
        "chat": {"id": 42},
        "voice": {"file_id": "AwACAgEAAx", "duration": 3},
    },
}
TEXT_UPDATE = {
    "update_id": 8,
    "message": {"chat": {"id": 42}, "text": "hello"},
}


# -- multipart encoder ------------------------------------------------------
def test_multipart_encoder_valid():
    body, ctype = _encode_multipart(
        {"model": "whisper-1"},
        {"file": ("voice.ogg", b"\x00OGGDATA", "audio/ogg")},
        boundary="TESTBOUND",
    )
    assert b"--TESTBOUND\r\n" in body
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'filename="voice.ogg"' in body
    assert b"\x00OGGDATA" in body
    assert body.endswith(b"--TESTBOUND--\r\n")
    assert ctype == "multipart/form-data; boundary=TESTBOUND"


def test_multipart_encoder_random_boundary_unique():
    b1, c1 = _encode_multipart({}, {"f": ("a", b"x", "text/plain")})
    b2, c2 = _encode_multipart({}, {"f": ("a", b"x", "text/plain")})
    assert c1 != c2  # random per call


# -- parse_voice_update -------------------------------------------------------
def test_parse_voice_update_ok():
    assert parse_voice_update(VOICE_UPDATE) == ("42", "AwACAgEAAx")


def test_parse_voice_update_ignores_text():
    assert parse_voice_update(TEXT_UPDATE) is None


def test_parse_voice_update_garbage():
    assert parse_voice_update({}) is None
    assert parse_voice_update(None) is None
    assert parse_voice_update({"message": {"chat": {"id": 1}}}) is None


# -- transcribe ---------------------------------------------------------------
def _ogg_file(tmp_path):
    p = tmp_path / "note.ogg"
    p.write_bytes(b"\x00FAKEOGG")
    return p


def test_transcribe_parses_text(fake_http, tmp_path):
    fake_http.add(
        lambda u, b: "audio/transcriptions" in u,
        lambda u, b: {"text": "hello world"},
    )
    text = transcribe_ogg(_ogg_file(tmp_path), openai_key="sk-test-key")
    assert text == "hello world"
    url, headers, body = fake_http.requests[0]
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert b"whisper-1" in body
    assert b"voice.ogg" in body


def test_transcribe_no_key(no_openai_key, tmp_path):
    with pytest.raises(VoiceError, match="OpenAI API key"):
        transcribe_ogg(_ogg_file(tmp_path), openai_key=None)


def test_transcribe_http_error_hides_key(fake_http, tmp_path):
    def boom(url, body):
        return urllib.error.HTTPError(
            url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"bad key"}}')
        )

    fake_http.add(lambda u, b: "audio/transcriptions" in u, boom)
    with pytest.raises(VoiceError) as ei:
        transcribe_ogg(_ogg_file(tmp_path), openai_key="sk-super-secret-key")
    msg = str(ei.value)
    assert "sk-super-secret-key" not in msg
    assert "401" in msg and "bad key" in msg


def test_transcribe_empty_text(fake_http, tmp_path):
    fake_http.add(lambda u, b: "audio/transcriptions" in u, lambda u, b: {"text": "  "})
    with pytest.raises(VoiceError, match="empty"):
        transcribe_ogg(_ogg_file(tmp_path), openai_key="sk-test-key")


# -- speak ----------------------------------------------------------------------
def test_speak_returns_bytes(fake_http):
    fake_http.add(
        lambda u, b: "audio/speech" in u, lambda u, b: b"ID3MP3BYTES"
    )
    out = speak("hello there", openai_key="sk-test-key")
    assert out == b"ID3MP3BYTES"
    url, headers, body = fake_http.requests[0]
    assert headers["Authorization"] == "Bearer sk-test-key"
    payload = json.loads(body.decode("utf-8"))
    assert payload["model"] == "tts-1"
    assert payload["input"] == "hello there"
    assert payload["voice"] == "alloy"


def test_speak_clips_long_text(fake_http):
    fake_http.add(lambda u, b: "audio/speech" in u, lambda u, b: b"MP3")
    speak("x" * 9000, openai_key="sk-test-key")
    _, _, body = fake_http.requests[0]
    payload = json.loads(body.decode("utf-8"))
    assert len(payload["input"]) <= voice_mod.TTS_MAX_CHARS


def test_speak_no_key(no_openai_key):
    with pytest.raises(VoiceError, match="OpenAI API key"):
        speak("hi", openai_key=None)


def test_speak_http_error(fake_http):
    def boom(url, body):
        return urllib.error.HTTPError(url, 429, "Rate limited", {}, io.BytesIO(b"{}"))

    fake_http.add(lambda u, b: "audio/speech" in u, boom)
    with pytest.raises(VoiceError, match="429"):
        speak("hi", openai_key="sk-test-key")


# -- send_voice_note --------------------------------------------------------------
def test_send_voice_note_posts_send_audio(fake_http):
    fake_http.add(
        lambda u, b: u.endswith("/sendAudio"), lambda u, b: {"ok": True, "result": {}}
    )
    bot = FakeBot()
    send_voice_note(bot, "42", b"MP3DATA", token="TESTTOKEN")
    url, headers, body = fake_http.requests[0]
    assert url == "https://api.telegram.org/botTESTTOKEN/sendAudio"
    assert "multipart/form-data" in headers["Content-type"]
    assert b"MP3DATA" in body
    assert b'name="chat_id"' in body and b"42" in body


def test_send_voice_note_empty_audio():
    with pytest.raises(VoiceError, match="empty audio"):
        send_voice_note(FakeBot(), "42", b"", token="T")


# -- download_voice ---------------------------------------------------------------
def test_download_voice(tmp_path, fake_http):
    fake_http.add(
        lambda u, b: "/file/bot" in u, lambda u, b: b"\x00OGGVOICE"
    )
    bot = FakeBot(getfile_result={"file_path": "voice/abc.ogg"})
    path = download_voice(bot, "file123", tmp_path, token="TESTTOKEN")
    assert path.suffix == ".ogg"
    assert path.read_bytes() == b"\x00OGGVOICE"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert bot.post_calls[0] == ("getFile", {"file_id": "file123"})
    url, _, _ = fake_http.requests[0]
    assert url == "https://api.telegram.org/file/botTESTTOKEN/voice/abc.ogg"


def test_download_voice_no_file_path():
    bot = FakeBot(getfile_result={})
    with pytest.raises(VoiceError, match="didn't return a file"):
        download_voice(bot, "file123", "/tmp", token="T")


# -- handle_voice_message pipeline --------------------------------------------------
def test_handle_voice_message_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(
        voice_mod, "download_voice", lambda bot, fid, d, token=None: tmp_path / "v.ogg"
    )
    monkeypatch.setattr(
        voice_mod, "transcribe_ogg", lambda path, key=None: "do the thing"
    )
    text = handle_voice_message(FakeBot(), VOICE_UPDATE, openai_key="sk-k")
    assert text == "do the thing"


def test_handle_voice_message_transcribe_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        voice_mod, "download_voice", lambda bot, fid, d, token=None: tmp_path / "v.ogg"
    )

    def boom(path, key=None):
        raise VoiceError("bad audio")

    monkeypatch.setattr(voice_mod, "transcribe_ogg", boom)
    assert handle_voice_message(FakeBot(), VOICE_UPDATE, openai_key="sk-k") is None
    assert "transcription failed" in capsys.readouterr().err
    assert TRANSCRIBE_FALLBACK  # caller-visible fallback exists


def test_handle_voice_message_not_a_voice_update():
    assert handle_voice_message(FakeBot(), TEXT_UPDATE, openai_key="sk-k") is None


# -- resolve_voice_key ---------------------------------------------------------------
def test_resolve_voice_key_explicit_openai(monkeypatch):
    monkeypatch.setattr(voice_mod._config, "get_key", lambda p: None)
    assert resolve_voice_key("sk-openai123") == "sk-openai123"
    assert resolve_voice_key("sk-openai123", provider_hint="openai") == "sk-openai123"


def test_resolve_voice_key_rejects_anthropic_key(monkeypatch):
    monkeypatch.setattr(
        voice_mod._config, "get_key", lambda p: "sk-config-openai" if p == "openai" else None
    )
    # Anthropic-shaped explicit key must NOT be used for OpenAI audio.
    assert resolve_voice_key("sk-ant-xyz") == "sk-config-openai"


def test_resolve_voice_key_none_when_nothing(monkeypatch):
    monkeypatch.setattr(voice_mod._config, "get_key", lambda p: None)
    assert resolve_voice_key() is None


# -- run.py voice branch -----------------------------------------------------------------
def _patch_voice_pipeline(monkeypatch, transcribed="do the thing"):
    monkeypatch.setattr(
        voice_mod, "handle_voice_message", lambda bot, upd, openai_key=None, token=None: transcribed
    )


def test_handle_update_voice_reply_as_voice(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_voice_pipeline(monkeypatch)
    monkeypatch.setattr(voice_mod, "speak", lambda text, key=None: b"MP3REPLY")
    sent_audio = []
    monkeypatch.setattr(
        voice_mod, "send_voice_note", lambda bot, cid, audio, filename="reply.mp3", token=None: sent_audio.append((cid, audio))
    )
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=True, openai_key="sk-k")
    assert handle_update(bot, loop, store, VOICE_UPDATE, voice=cfg) is True
    assert sent_audio == [("42", b"MP3REPLY")]
    assert len(bot.sent) == 1  # only the pairing notice, no text fallback
    assert bot.sent[0][1].startswith("🔗 Paired!")  # pairing notice first
    assert loop.seen_events[0].meta.get("via") == "voice"


def test_handle_update_voice_speak_fails_falls_back_to_text(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_voice_pipeline(monkeypatch)

    def boom(text, key=None):
        raise VoiceError("tts down")

    monkeypatch.setattr(voice_mod, "speak", boom)
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=True, openai_key="sk-k")
    assert handle_update(bot, loop, store, VOICE_UPDATE, voice=cfg) is True
    assert bot.sent[0][1].startswith("🔗 Paired!")  # pairing notice first
    assert bot.sent[1] == ("42", "FINAL: done")


def test_handle_update_voice_no_key_text_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_voice_pipeline(monkeypatch)
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=True, openai_key=None)
    assert handle_update(bot, loop, store, VOICE_UPDATE, voice=cfg) is True
    assert bot.sent[0][1].startswith("🔗 Paired!")  # pairing notice first
    assert bot.sent[1] == ("42", "FINAL: done")


def test_handle_update_voice_transcribe_none_sends_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_voice_pipeline(monkeypatch, transcribed=None)
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=True, openai_key="sk-k")
    assert handle_update(bot, loop, store, VOICE_UPDATE, voice=cfg) is True
    assert bot.sent[0][1].startswith("🔗 Paired!")  # pairing notice first
    assert bot.sent[1] == ("42", TRANSCRIBE_FALLBACK)
    assert loop.seen_events == []  # agent never ran


def test_handle_update_voice_disabled_ignores_voice(monkeypatch):
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=False)
    assert handle_update(bot, loop, store, VOICE_UPDATE, voice=cfg) is False


def test_handle_update_text_still_works_with_voice_cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    _patch_voice_pipeline(monkeypatch)
    bot, loop, store = FakeSendBot(), FakeLoop(), ConversationStore()
    cfg = VoiceConfig(enabled=True, openai_key="sk-k")
    assert handle_update(bot, loop, store, TEXT_UPDATE, voice=cfg) is True
    assert bot.sent[0][1].startswith("🔗 Paired!")  # pairing notice first
    assert bot.sent[1] == ("42", "FINAL: done")


def test_voice_flag_defaults_on():
    args = build_parser().parse_args([])
    assert args.voice is True
    args = build_parser().parse_args(["--no-voice"])
    assert args.voice is False
