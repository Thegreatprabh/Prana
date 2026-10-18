"""Tests for the WhatsApp channel — fully mocked HTTP, zero network.

urlopen is faked at the urllib layer so WhatsAppClient exercises its real
request/response/error paths against canned payloads. The webhook listener
is tested against a real localhost HTTP server (no external network).
"""

import io
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from prana.channels.whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    parse_webhook,
    resolve_phone_number_id,
    resolve_verify_token,
    resolve_whatsapp_token,
    verify_webhook,
)
from prana.channels.whatsapp_run import (
    ConversationStore,
    handle_message,
    make_handler,
)
from prana.loop import AgentLoop
from prana.providers.base import Completion, Provider
from prana.tools import ToolRegistry

FAKE_TOKEN = "wa-test-token-123"
FAKE_PID = "123456789012345"

#: The real urlopen, captured before any fixture monkeypatches it — the
#: localhost webhook-server tests must talk to the real loopback server.
_REAL_OPEN = urllib.request.urlopen


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


class FakeWhatsAppHTTP:
    """Scripted stand-in for graph.facebook.com. Records every request."""

    def __init__(self, fail_with=None):
        self.requests = []  # (url, payload, auth_header)
        self.fail_with = fail_with

    def __call__(self, req, timeout=None):
        url = req.full_url
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        # The real header is added via add_unredirected_header; the visible
        # headers dict keeps the redacted placeholder (by design).
        auth = req.get_header("Authorization")
        self.requests.append((url, payload, auth))
        if self.fail_with is not None:
            raise self.fail_with
        return FakeHTTPResponse({"messages": [{"id": "wamid.test"}]})


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeWhatsAppHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def client():
    return WhatsAppClient(FAKE_TOKEN, FAKE_PID)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "WHATSAPP_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_VERIFY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)


# -- webhook verification --------------------------------------------------
def test_verify_good_token_returns_challenge():
    qs = {
        "hub.mode": "subscribe",
        "hub.verify_token": "secret123",
        "hub.challenge": "CHALLENGE-ABC",
    }
    assert verify_webhook(qs, "secret123") == "CHALLENGE-ABC"


def test_verify_bad_token_rejected():
    qs = {
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "CHALLENGE-ABC",
    }
    assert verify_webhook(qs, "secret123") is None


def test_verify_wrong_mode_rejected():
    qs = {"hub.mode": "unsubscribe", "hub.verify_token": "secret123",
          "hub.challenge": "X"}
    assert verify_webhook(qs, "secret123") is None


def test_verify_empty_config_token_rejects_everything():
    qs = {"hub.mode": "subscribe", "hub.verify_token": "anything",
          "hub.challenge": "X"}
    assert verify_webhook(qs, "") is None


def test_verify_missing_challenge_rejected():
    qs = {"hub.mode": "subscribe", "hub.verify_token": "secret123"}
    assert verify_webhook(qs, "secret123") is None


# -- webhook payload parsing -----------------------------------------------
def _sample_payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "111",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.1",
                                    "type": "text",
                                    "text": {"body": "hello prana"},
                                },
                                {
                                    "from": "919876543210",
                                    "id": "wamid.2",
                                    "type": "image",
                                    "image": {"id": "img1"},
                                },
                            ],
                        },
                        "field": "messages",
                    },
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {"id": "wamid.1", "status": "delivered"}
                            ],
                        },
                        "field": "messages",
                    },
                ],
            }
        ],
    }


def test_parse_webhook_extracts_text_ignores_rest():
    parsed = parse_webhook(_sample_payload())
    assert parsed == [("919876543210", "hello prana")]  # image + statuses skipped


def test_parse_webhook_statuses_only_empty():
    payload = {"entry": [{"changes": [{"value": {"statuses": []}}]}]}
    assert parse_webhook(payload) == []


@pytest.mark.parametrize("junk", [None, {}, [], "nope", {"entry": "nope"},
                                  {"entry": [{"changes": None}]}])
def test_parse_webhook_malformed_never_raises(junk):
    assert parse_webhook(junk) == []


def test_parse_webhook_empty_text_ignored():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "123", "type": "text", "text": {"body": "   "}}]}}]}]}
    assert parse_webhook(payload) == []


# -- credential resolution ---------------------------------------------------
def test_resolve_token_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "env-token")
    assert resolve_whatsapp_token() == "env-token"


def test_resolve_phone_id_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "pid-1")
    assert resolve_phone_number_id() == "pid-1"


def test_resolve_verify_token_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    assert resolve_verify_token() == "vt"


def test_missing_token_clean_error(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # empty config dir
    with pytest.raises(WhatsAppError) as exc:
        resolve_whatsapp_token()
    assert "WHATSAPP_TOKEN" in str(exc.value)


def test_missing_phone_id_clean_error(clean_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(WhatsAppError) as exc:
        resolve_phone_number_id()
    assert "phone_number_id" in str(exc.value)


def test_token_never_in_repr(client):
    assert FAKE_TOKEN not in repr(client)
    assert "<redacted>" in repr(client)


def test_token_never_in_error_text(client, monkeypatch):
    err = urllib.error.HTTPError(
        url="https://x", code=401, msg="Unauthorized", hdrs=None,
        fp=io.BytesIO(b'{"error": {"message": "bad auth"}}'),
    )
    monkeypatch.setattr(urllib.request, "urlopen", FakeWhatsAppHTTP(fail_with=err))
    with pytest.raises(WhatsAppError) as exc:
        client.send_message("123", "hi")
    assert FAKE_TOKEN not in str(exc.value)
    assert "401" in str(exc.value)


# -- egress ------------------------------------------------------------------
def test_send_message_payload_shape(client, fake_http):
    client.send_message("919876543210", "hello")
    assert len(fake_http.requests) == 1
    url, payload, auth = fake_http.requests[0]
    assert f"/{FAKE_PID}/messages" in url
    assert payload["messaging_product"] == "whatsapp"
    assert payload["to"] == "919876543210"
    assert payload["type"] == "text"
    assert payload["text"]["body"] == "hello"
    assert auth == f"Bearer {FAKE_TOKEN}"  # real header on the wire
    # ...and the token never appears in the JSON body
    assert FAKE_TOKEN not in str(payload)


def test_send_message_chunks_long_text(client, fake_http):
    client.send_message("123", "x" * 5000)
    assert len(fake_http.requests) == 2
    bodies = [p["text"]["body"] for _, p, _ in fake_http.requests]
    assert all(len(b) <= 4000 for b in bodies)
    assert "".join(bodies) == "x" * 5000


def test_message_to_event(client):
    ev = client.message_to_event("919876543210", "hi")
    assert ev.kind == "user_message"
    assert ev.text == "hi"
    assert ev.meta["channel"] == "whatsapp"
    assert ev.meta["chat_id"] == "919876543210"


# -- scripted agent ------------------------------------------------------------
class ScriptedProvider(Provider):
    name = "fake"
    default_model = "fake-1"

    def __init__(self, script):
        super().__init__(api_key="<redacted>")
        self.script = list(script)

    def complete(self, messages, tools, **kwargs):
        return self.script.pop(0) if self.script else Completion(text="done")


def _demo_loop(tmp_path):
    reg = ToolRegistry()
    provider = ScriptedProvider([Completion(text="FINAL: whatsapp works ✅")])
    return AgentLoop(provider, reg, max_iters=3)


def test_handle_message_end_to_end(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    loop = _demo_loop(tmp_path)
    store = ConversationStore()
    assert handle_message(client, loop, store, "919876543210", "hello prana") is True
    assert len(fake_http.requests) == 2
    assert "🔗 Paired!" in fake_http.requests[0][1]["text"]["body"]  # pairing notice first
    _, payload, _ = fake_http.requests[1]
    assert payload["to"] == "919876543210"
    assert "whatsapp works" in payload["text"]["body"]
    # history recorded both sides
    assert store.context_block("919876543210") != ""


def test_handle_message_help_is_local(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    loop = _demo_loop(tmp_path)  # script must NOT be consumed
    store = ConversationStore()
    assert handle_message(client, loop, store, "123", "help") is True
    assert "🔗 Paired!" in fake_http.requests[0][1]["text"]["body"]  # pairing notice first
    _, payload, _ = fake_http.requests[1]
    assert "PRANA commands" in payload["text"]["body"]
    assert len(loop.provider.script) == 1  # no LLM call happened


def test_handle_message_blank_ignored(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    loop = _demo_loop(tmp_path)
    assert handle_message(client, loop, store := ConversationStore(), "123", "   ") is False
    assert fake_http.requests == []


# -- live localhost webhook server ---------------------------------------------
def _run_test_server(client, loop, verify_token="secret123", app_secret=""):
    store = ConversationStore()
    handler = make_handler(client, loop, store, "/webhook", verify_token,
                           app_secret=app_secret)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _http_get(port, path):
    with _REAL_OPEN(f"http://127.0.0.1:{port}{path}") as r:
        return r.status, r.read()


def _http_post(port, path, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with _REAL_OPEN(req) as r:
        return r.status, r.read()


def test_webhook_get_verify_roundtrip(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path))
    try:
        status, body = _http_get(
            port, "/webhook?hub.mode=subscribe&hub.verify_token=secret123"
                 "&hub.challenge=CHALLENGE-1")
        assert status == 200 and body == b"CHALLENGE-1"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _http_get(port, "/webhook?hub.mode=subscribe&hub.verify_token=nope"
                             "&hub.challenge=X")
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            _http_get(port, "/nope")
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_webhook_post_message_gets_reply(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path))
    try:
        status, body = _http_post(port, "/webhook", _sample_payload())
        assert status == 200 and body == b"OK"
        # text message got an agent reply; image/status did not
        assert len(fake_http.requests) == 2
        assert "🔗 Paired!" in fake_http.requests[0][1]["text"]["body"]  # pairing notice first
        _, payload, _ = fake_http.requests[1]
        assert payload["to"] == "919876543210"
        assert "whatsapp works" in payload["text"]["body"]
    finally:
        server.shutdown()


def _sign_body(body: bytes, secret: str) -> str:
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode("utf-8"), body,
                                hashlib.sha256).hexdigest()


def _http_post_signed(port, path, payload, secret):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=body,
        headers={"Content-Type": "application/json",
                 "X-Hub-Signature-256": _sign_body(body, secret)},
        method="POST",
    )
    with _REAL_OPEN(req) as r:
        return r.status, r.read()


def test_verify_signature_real_hmac_vector():
    from prana.channels.whatsapp import verify_signature
    body = b'{"hello": "world"}'
    good = _sign_body(body, "s3cr3t")
    assert verify_signature(body, good, "s3cr3t") is True
    assert verify_signature(body, good, "wrong-secret") is False
    assert verify_signature(body, "sha256=" + "0" * 64, "s3cr3t") is False
    assert verify_signature(body, "not-a-signature", "s3cr3t") is False
    assert verify_signature(b"", good, "s3cr3t") is False
    assert verify_signature(body, "", "s3cr3t") is False
    assert verify_signature(body, good, "") is False


def test_webhook_post_good_signature_accepted(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path),
                                    app_secret="s3cr3t")
    try:
        status, body = _http_post_signed(port, "/webhook", _sample_payload(),
                                         "s3cr3t")
        assert status == 200 and body == b"OK"
        # pairing notice + agent reply
        assert len(fake_http.requests) == 2
        _, payload, _ = fake_http.requests[1]
        assert "whatsapp works" in payload["text"]["body"]
    finally:
        server.shutdown()


def test_webhook_post_bad_signature_403(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path),
                                    app_secret="s3cr3t")
    try:
        body = json.dumps(_sample_payload()).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook", data=body,
            headers={"Content-Type": "application/json",
                     "X-Hub-Signature-256": "sha256=" + "f" * 64},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            _REAL_OPEN(req)
        assert exc.value.code == 403
        assert fake_http.requests == []  # nothing processed, nothing sent
    finally:
        server.shutdown()


def test_webhook_post_missing_signature_403(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path),
                                    app_secret="s3cr3t")
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _http_post(port, "/webhook", _sample_payload())  # no signature header
        assert exc.value.code == 403
        assert fake_http.requests == []
    finally:
        server.shutdown()


def test_resolve_app_secret_env_first(tmp_path, monkeypatch):
    from prana.channels.whatsapp import resolve_app_secret
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    assert resolve_app_secret() == ""
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "env-secret")
    assert resolve_app_secret() == "env-secret"


def test_webhook_post_bad_json_400(tmp_path, fake_http, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/webhook", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            _REAL_OPEN(req)
        assert exc.value.code == 400
    finally:
        server.shutdown()


def test_verify_token_scrubbed_from_logs(tmp_path, fake_http, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = WhatsAppClient(FAKE_TOKEN, FAKE_PID)
    server, port = _run_test_server(client, _demo_loop(tmp_path),
                                    verify_token="super-secret-vt")
    try:
        _http_get(port, "/webhook?hub.mode=subscribe&hub.verify_token=super-secret-vt"
                         "&hub.challenge=C")
    finally:
        server.shutdown()
    logged = capsys.readouterr().err
    assert "super-secret-vt" not in logged
    assert "hub.verify_token=<redacted>" in logged
