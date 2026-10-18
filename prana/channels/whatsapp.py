"""WhatsApp channel adapter — Meta WhatsApp Business Cloud API over stdlib urllib.

No third-party dependencies. Two halves:

- Egress: POST text messages to
  ``https://graph.facebook.com/v22.0/<PHONE_NUMBER_ID>/messages``.
- Ingress: Meta calls OUR webhook (GET verify + POST message payloads);
  ``prana.channels.whatsapp_run`` serves that listener.

Credentials resolve from ``WHATSAPP_TOKEN`` / ``WHATSAPP_PHONE_NUMBER_ID``
env vars first, then ``~/.prana/config.json``::

    {"whatsapp": {"token": "...", "phone_number_id": "..."}}

The token is NEVER logged, never interpolated into error text, and files
holding it are 0600.

Human setup (Meta developer app — about 10 minutes, free tier):
  1. Go to developers.facebook.com → My Apps → Create App → "Business".
  2. Add the "WhatsApp" product to the app.
  3. In WhatsApp → API Setup: copy the temporary access token and the
     Phone number ID. (For a permanent token: create a system user in
     Business Settings and assign whatsapp_business_messaging.)
  4. Start the listener: ``prana-whatsapp --port 8000`` — then expose it
     with a tunnel (``ngrok http 8000`` or ``cloudflared tunnel --url
     http://localhost:8000``) because Meta needs a PUBLIC HTTPS URL.
  5. In WhatsApp → Configuration → Webhook: Callback URL =
     ``https://<your-tunnel>/webhook``, Verify token = anything you choose
     (set the same value as WHATSAPP_VERIFY_TOKEN), then Subscribe to the
     ``messages`` field.
  6. Send a WhatsApp message to the test number — PRANA replies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .. import config as _config
from ..events import Event, user_message
from .telegram import chunk_text

WHATSAPP_API_BASE = "https://graph.facebook.com/v22.0"
#: WhatsApp Cloud API text cap is 4096 chars; stay under with margin.
MAX_MESSAGE_CHARS = 4000
TOKEN_ENV_VAR = "WHATSAPP_TOKEN"
PHONE_NUMBER_ID_ENV_VAR = "WHATSAPP_PHONE_NUMBER_ID"
VERIFY_TOKEN_ENV_VAR = "WHATSAPP_VERIFY_TOKEN"
APP_SECRET_ENV_VAR = "WHATSAPP_APP_SECRET"


class WhatsAppError(Exception):
    """WhatsApp API / configuration failure. Never contains the token."""


def _config_section() -> Dict[str, Any]:
    data = _config.load_config().get("whatsapp")
    return data if isinstance(data, dict) else {}


def resolve_whatsapp_token() -> str:
    """Find the Cloud API token: env var first, then config file."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    token = str(_config_section().get("token") or "").strip()
    if token:
        return token
    raise WhatsAppError(
        "no WhatsApp token — set WHATSAPP_TOKEN, or add "
        '"whatsapp": {"token": "...", "phone_number_id": "..."} to '
        "~/.prana/config.json (see module docstring for the 10-minute setup)"
    )


def resolve_phone_number_id() -> str:
    """Find the sender's Phone Number ID: env var first, then config file."""
    pid = os.environ.get(PHONE_NUMBER_ID_ENV_VAR, "").strip()
    if pid:
        return pid
    pid = str(_config_section().get("phone_number_id") or "").strip()
    if pid:
        return pid
    raise WhatsAppError(
        "no WhatsApp phone_number_id — set WHATSAPP_PHONE_NUMBER_ID, or add "
        '"phone_number_id" under "whatsapp" in ~/.prana/config.json '
        "(Meta app dashboard → WhatsApp → API Setup)"
    )


def resolve_verify_token() -> str:
    """Webhook verify token: env var first, then config file (may be empty)."""
    token = os.environ.get(VERIFY_TOKEN_ENV_VAR, "").strip()
    if token:
        return token
    return str(_config_section().get("verify_token") or "").strip()


def resolve_app_secret() -> str:
    """Meta app secret for X-Hub-Signature-256 verification (may be empty).

    Without it, webhook POSTs cannot be authenticated — the listener warns
    at startup. Get it from the Meta app dashboard → Settings → Basic.
    """
    secret = os.environ.get(APP_SECRET_ENV_VAR, "").strip()
    if secret:
        return secret
    return str(_config_section().get("app_secret") or "").strip()


def verify_signature(raw_body: bytes, signature_header: str, app_secret: str) -> bool:
    """Check Meta's ``X-Hub-Signature-256: sha256=<hmac>`` header.

    HMAC-SHA256 over the raw request body, compared with hmac.compare_digest.
    Any missing/empty input → False (fail closed).
    """
    if not raw_body or not signature_header or not app_secret:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + expected, signature_header)


def verify_webhook(query_params: Dict[str, str], verify_token: str) -> Optional[str]:
    """Meta's webhook verification handshake.

    Meta GETs ``?hub.mode=subscribe&hub.verify_token=<ours>&hub.challenge=<x>``.
    Return the challenge to echo back on success, None to reject (→ 403).
    """
    if query_params.get("hub.mode") != "subscribe":
        return None
    if not verify_token:
        return None
    if query_params.get("hub.verify_token") != verify_token:
        return None
    challenge = query_params.get("hub.challenge")
    return challenge if challenge else None


def _iter_messages(value: Any):
    """Yield raw message dicts from a webhook ``value`` object."""
    if not isinstance(value, dict):
        return
    messages = value.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if isinstance(msg, dict):
            yield msg


def parse_webhook(payload: Any) -> List[Tuple[str, str]]:
    """Extract ``(from_number, text)`` pairs from a Cloud API webhook payload.

    Walks entry → changes → value → messages. Delivery receipts
    (``value.statuses``), non-text messages (images, audio, reactions…),
    and malformed payloads are ignored. Never raises — returns [] on junk.
    """
    results: List[Tuple[str, str]] = []
    try:
        if not isinstance(payload, dict):
            return results
        entries = payload.get("entry")
        if not isinstance(entries, list):
            return results
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes")
            if not isinstance(changes, list):
                continue
            for change in changes:
                if not isinstance(change, dict):
                    continue
                value = change.get("value")
                for msg in _iter_messages(value):
                    if msg.get("type") != "text":
                        continue
                    text_obj = msg.get("text")
                    body = text_obj.get("body") if isinstance(text_obj, dict) else None
                    sender = msg.get("from")
                    if (
                        isinstance(sender, str)
                        and sender
                        and isinstance(body, str)
                        and body.strip()
                    ):
                        results.append((sender, body))
    except Exception:
        return []
    return results


def _read_http_error_body(err: urllib.error.HTTPError) -> str:
    try:
        body = err.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:200]
        return body[:200]
    except Exception:
        return ""


class WhatsAppClient:
    """Thin wrapper around the WhatsApp Cloud API (urllib only)."""

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_base: str = WHATSAPP_API_BASE,
        timeout: int = 60,
    ) -> None:
        if not token or not token.strip():
            raise WhatsAppError("empty WhatsApp token")
        if not phone_number_id or not phone_number_id.strip():
            raise WhatsAppError("empty WhatsApp phone_number_id")
        self._token = token
        self._phone_number_id = phone_number_id.strip()
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def __repr__(self) -> str:  # token never leaks via repr
        return (
            f"WhatsAppClient(api_base={self._api_base!r}, "
            f"phone_number_id={self._phone_number_id!r}, token=<redacted>)"
        )

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        """POST JSON with Bearer auth. Errors name the path only — never the token.

        The token travels only in the Authorization header of this request;
        PRANA never logs Request objects, so it cannot leak into logs.
        """
        url = f"{self._api_base}/{self._phone_number_id}/{path.lstrip('/')}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._token}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = _read_http_error_body(e)
            raise WhatsAppError(
                f"WhatsApp API HTTP {e.code} on {path}: {detail or 'no detail'}"
            )
        except urllib.error.URLError as e:
            raise WhatsAppError(f"WhatsApp API unreachable on {path}: {e.reason}")
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise WhatsAppError(f"WhatsApp API bad response on {path}: {e}")
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message", "unknown error") if isinstance(err, dict) else err
            raise WhatsAppError(f"WhatsApp API error on {path}: {msg}")
        return data

    # -- API methods -------------------------------------------------------
    def send_message(self, to: str, text: str) -> List[Any]:
        """Send text to a WhatsApp number, chunked to fit API limits."""
        results = []
        for chunk in chunk_text(text, MAX_MESSAGE_CHARS):
            payload = {
                "messaging_product": "whatsapp",
                "to": str(to),
                "type": "text",
                "text": {"body": chunk, "preview_url": False},
            }
            results.append(self._post("messages", payload))
        return results

    # -- ingress -----------------------------------------------------------
    def message_to_event(self, from_number: str, text: str) -> Event:
        """Convert one inbound WhatsApp message into a PRANA user_message event."""
        return user_message(
            text,
            channel="whatsapp",
            chat_id=str(from_number),
        )
