"""Telegram voice notes — the pragmatic v1 of "voice calls" for PRANA.

Pipeline: user sends a Telegram voice message → we download the .ogg via
getFile → OpenAI Whisper transcribes it → the agent thinks → the reply goes
back as a playable audio message (or plain text when TTS isn't available).

Honest scope: real phone calls need Twilio (paid account) — out of scope.
This is voice *notes*, using the user's own BYOK OpenAI key (the same key
PRANA already uses). No new accounts, no new vendors.

Stdlib only. The API key is never logged, never interpolated into error
text, and never written to disk by this module.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import config as _config
from ..providers.detect import ProviderError, detect_provider
from .telegram import (
    TELEGRAM_API_BASE,
    TelegramBot,
    TelegramError,
    resolve_telegram_token,
)

OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_SPEECH_URL = "https://api.openai.com/v1/audio/speech"
WHISPER_MODEL = "whisper-1"
TTS_MODEL = "tts-1"
TTS_VOICE = "alloy"
#: tts-1 caps input at 4096 chars; we stay under with margin.
TTS_MAX_CHARS = 4000
#: What the user sees when a voice note can't be transcribed.
TRANSCRIBE_FALLBACK = (
    "🎙️ I couldn't transcribe that voice note — voice needs an OpenAI API key "
    "(the same one PRANA uses for chat). Run `prana --save-key` with an OpenAI "
    "key, or just send your message as text."
)


class VoiceError(Exception):
    """Voice pipeline failure. Never contains an API key."""


@dataclass
class VoiceConfig:
    """Voice-note behavior for the Telegram poll loop."""

    enabled: bool = True
    openai_key: Optional[str] = None  # None → voice replies fall back to text


# -- multipart/form-data (hand-rolled, stdlib only) --------------------------
def _encode_multipart(
    fields: Dict[str, str],
    files: Dict[str, Tuple[str, bytes, str]],
    boundary: Optional[str] = None,
) -> Tuple[bytes, str]:
    """Encode multipart/form-data by hand.

    files: field name -> (filename, raw bytes, content type).
    Returns (body bytes, Content-Type header value).
    """
    if boundary is None:
        boundary = "----prana" + secrets.token_hex(16)
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\n".encode("ascii"))
        buf.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii")
        )
        buf.write(value.encode("utf-8"))
        buf.write(b"\r\n")
    for name, (filename, data, ctype) in files.items():
        buf.write(f"--{boundary}\r\n".encode("ascii"))
        buf.write(
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'.encode("ascii")
        )
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode("ascii"))
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("ascii"))
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _openai_error_body(err: urllib.error.HTTPError) -> str:
    """Extract a safe error detail from an OpenAI HTTPError (never the key)."""
    try:
        body = err.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if isinstance(data, dict):
            inner = data.get("error")
            if isinstance(inner, dict) and inner.get("message"):
                return str(inner["message"])[:200]
            if isinstance(inner, str):
                return inner[:200]
        return body[:200]
    except Exception:
        return ""


def _openai_post(
    url: str,
    key: str,
    body: bytes,
    content_type: str,
    timeout: int,
    what: str,
) -> bytes:
    """POST to the OpenAI API. Errors name the operation only — never the key."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": content_type,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = _openai_error_body(e)
        raise VoiceError(f"{what} failed (HTTP {e.code}): {detail or 'no detail'}")
    except urllib.error.URLError as e:
        raise VoiceError(f"{what} unreachable: {e.reason}")


def resolve_voice_key(
    explicit_key: Optional[str] = None,
    provider_hint: Optional[str] = None,
) -> Optional[str]:
    """Find an OpenAI key for Whisper/TTS.

    Prefers an explicit --key when it is OpenAI-shaped (or --provider openai
    was given), else falls back to OPENAI_API_KEY env / ~/.prana/config.json.
    Returns None when no OpenAI key is configured — callers degrade to text.
    """
    if explicit_key and explicit_key.strip():
        key = explicit_key.strip()
        if provider_hint == "openai":
            return key
        if not provider_hint:
            try:
                if detect_provider(key) == "openai":
                    return key
            except ProviderError:
                pass
    return _config.get_key("openai")


# -- Telegram voice ingress --------------------------------------------------
def parse_voice_update(update: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Extract (chat_id, file_id) from a voice-message update, else None."""
    if not isinstance(update, dict):
        return None
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    voice = msg.get("voice")
    if not isinstance(voice, dict):
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    file_id = voice.get("file_id")
    if chat_id is None or not file_id:
        return None
    return (str(chat_id), str(file_id))


def _telegram_file_url(file_path: str, token: Optional[str] = None) -> str:
    token = token or resolve_telegram_token()
    return f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"


def download_voice(
    bot: TelegramBot,
    file_id: str,
    dest_dir: str | Path,
    token: Optional[str] = None,
) -> Path:
    """Download a Telegram voice note (.ogg) via getFile. Returns the file path."""
    try:
        info = bot._post("getFile", {"file_id": file_id})
    except TelegramError as e:
        raise VoiceError(f"couldn't fetch the voice file from Telegram: {e}")
    file_path = info.get("file_path") if isinstance(info, dict) else None
    if not file_path:
        raise VoiceError("Telegram didn't return a file for that voice message")
    url = _telegram_file_url(str(file_path), token)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            # Cap download size: Telegram voice notes are small; an unbounded
            # read could exhaust memory on a rogue response.
            data = resp.read(25 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as e:
        raise VoiceError(f"voice download failed (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise VoiceError(f"voice download unreachable: {e.reason}")
    if not data:
        raise VoiceError("downloaded voice file is empty")
    if len(data) > 25 * 1024 * 1024:
        raise VoiceError("voice file too large (>25MB)")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    # file_id is Telegram-opaque; keep our own safe filename.
    out = dest / f"voice-{secrets.token_hex(8)}.ogg"
    out.write_bytes(data)
    os.chmod(out, 0o600)
    return out


# -- OpenAI audio -------------------------------------------------------------
def transcribe_ogg(path: str | Path, openai_key: Optional[str] = None) -> str:
    """Transcribe an .ogg voice note with Whisper. Returns the text."""
    key = openai_key or _config.get_key("openai")
    if not key:
        raise VoiceError(
            "voice transcription needs an OpenAI API key — run `prana --save-key` "
            "(the same key PRANA uses for chat)"
        )
    audio = Path(path).read_bytes()
    if not audio:
        raise VoiceError("voice file is empty — nothing to transcribe")
    body, ctype = _encode_multipart(
        {"model": WHISPER_MODEL},
        {"file": ("voice.ogg", audio, "audio/ogg")},
    )
    raw = _openai_post(
        OPENAI_TRANSCRIPTIONS_URL, key, body, ctype, 180, "transcription"
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise VoiceError("transcription returned an unreadable response")
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    if not isinstance(text, str) or not text.strip():
        raise VoiceError("transcription came back empty — try a clearer voice note")
    return text.strip()


def speak(
    text: str,
    openai_key: Optional[str] = None,
    voice: str = TTS_VOICE,
) -> bytes:
    """Text → speech (mp3 bytes) via OpenAI TTS."""
    key = openai_key or _config.get_key("openai")
    if not key:
        raise VoiceError(
            "voice replies need an OpenAI API key — run `prana --save-key` "
            "(the same key PRANA uses for chat)"
        )
    clipped = (text or "").strip()[:TTS_MAX_CHARS]
    if not clipped:
        raise VoiceError("nothing to say — empty reply text")
    body = json.dumps(
        {"model": TTS_MODEL, "input": clipped, "voice": voice}
    ).encode("utf-8")
    return _openai_post(OPENAI_SPEECH_URL, key, body, "application/json", 120, "speech")


def send_voice_note(
    bot: TelegramBot,
    chat_id: str,
    audio_bytes: bytes,
    filename: str = "reply.mp3",
    token: Optional[str] = None,
) -> Any:
    """Send an audio message via sendAudio (multipart). mp3 plays inline."""
    if not audio_bytes:
        raise VoiceError("nothing to send — empty audio")
    body, ctype = _encode_multipart(
        {"chat_id": str(chat_id)},
        {"audio": (filename, audio_bytes, "audio/mpeg")},
    )
    return _telegram_multipart("sendAudio", body, ctype, token)


def _telegram_multipart(
    method: str,
    body: bytes,
    content_type: str,
    token: Optional[str] = None,
) -> Any:
    """Multipart POST to the Bot API. Error text names the method, never the URL."""
    token = token or resolve_telegram_token()
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise VoiceError(f"Telegram {method} failed (HTTP {e.code})")
    except urllib.error.URLError as e:
        raise VoiceError(f"Telegram {method} unreachable: {e.reason}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise VoiceError(f"Telegram {method} returned an unreadable response")
    if not isinstance(data, dict) or not data.get("ok"):
        desc = (
            data.get("description", "unknown error")
            if isinstance(data, dict)
            else "unknown error"
        )
        raise VoiceError(f"Telegram {method} error: {desc}")
    return data.get("result")


# -- full pipeline --------------------------------------------------------------
def handle_voice_message(
    bot: TelegramBot,
    update: Dict[str, Any],
    *,
    openai_key: Optional[str] = None,
    token: Optional[str] = None,
) -> Optional[str]:
    """Download + transcribe a Telegram voice message.

    Returns the transcribed text, or None when voice can't be processed
    (reason goes to stderr; the caller replies with TRANSCRIBE_FALLBACK).
    Downloaded audio is deleted afterwards — never kept on disk.
    """
    parsed = parse_voice_update(update)
    if parsed is None:
        return None
    _chat_id, file_id = parsed
    tmpdir = tempfile.mkdtemp(prefix="prana-voice-")
    try:
        try:
            path = download_voice(bot, file_id, tmpdir, token=token)
        except (VoiceError, TelegramError) as e:
            print(f"voice: download failed: {e}", file=sys.stderr)
            return None
        try:
            return transcribe_ogg(path, openai_key)
        except VoiceError as e:
            print(f"voice: transcription failed: {e}", file=sys.stderr)
            return None
    finally:
        # Best-effort cleanup of the downloaded audio.
        try:
            for child in Path(tmpdir).iterdir():
                child.unlink()
            Path(tmpdir).rmdir()
        except OSError:
            pass


def voice_reply_targets() -> List[str]:
    """Human-readable summary of what v1 voice supports (for docs/help)."""
    return [
        "Telegram voice notes in → transcribed with Whisper",
        "Replies out as playable audio (OpenAI TTS) or text fallback",
        "Real phone calls need Twilio (paid) — not in v1",
    ]
