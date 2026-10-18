"""WhatsApp webhook listener — PRANA on WhatsApp (SPEC.md §2.6).

Meta calls OUR webhook: GET to verify the callback URL, then POST for every
inbound message. Each text message becomes a user_message Event, runs the
AgentLoop, and the reply goes back via the Cloud API. Per-number
conversation history gives continuity without touching the core loop.

PUBLIC HTTPS REQUIRED: the Cloud API cannot reach localhost. Run this
listener, then put a tunnel in front of it::

    prana-whatsapp --port 8000
    ngrok http 8000            # or: cloudflared tunnel --url http://localhost:8000

Then set the Meta app webhook Callback URL to
``https://<your-tunnel>/webhook`` (Subscribe to the ``messages`` field).
Full Meta-developer-app steps live in ``prana/channels/whatsapp.py``.

This is also the egress path for proactive pings: P3's delivery registry
can send to ``"whatsapp:<number>"`` targets once a ``whatsapp`` sink is
registered (mirror of the telegram sink in ``prana.channels.notify``).

Entry point: ``prana.channels.whatsapp_run:main`` (``prana-whatsapp``
console script).

Live use needs WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID (or the
``whatsapp`` section of ~/.prana/config.json).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .. import config, events, tokens
from ..loop import AgentLoop, DEFAULT_MAX_ITERS
from ..personality import MODES, load_identity
from ..providers.base import ProviderError
from ..providers.detect import resolve_provider
from ..tools import ToolRegistry, builtin_tools
from ..tools.memory_tools import builtin_memory_tools
from .whatsapp import (
    WhatsAppClient,
    WhatsAppError,
    parse_webhook,
    resolve_app_secret,
    resolve_phone_number_id,
    resolve_verify_token,
    resolve_whatsapp_token,
    verify_signature,
    verify_webhook,
)
from . import auth as auth_mod

INTRO = (
    "🫁 PRANA — give any chatbot a life.\n\n"
    "I'm an autonomous agent, not a chatbot. Give me a task and I'll work it "
    "with real tools — shell, files, web — then report back.\n\n"
    "Try: 'summarize what you can do' or just tell me what you need.\n"
    "Reply 'help' for commands."
)

HELP = (
    "PRANA commands\n"
    "help — this message\n"
    "start — intro\n\n"
    "Anything else is a task or a message for the agent. "
    "Long tasks may take a few tool steps; I'll report when done."
)

#: Rolling history kept per WhatsApp number for conversational continuity.
HISTORY_PER_CHAT = 40
#: Cap on the injected history block (chars) — token-frugal by design.
HISTORY_CONTEXT_CHARS = 6000
DEFAULT_PORT = 8000
DEFAULT_WEBHOOK_PATH = "/webhook"


class ConversationStore:
    """Per-number rolling (role, text) history, newest last."""

    def __init__(self, max_messages: int = HISTORY_PER_CHAT) -> None:
        self._hist: Dict[str, Deque[Tuple[str, str]]] = defaultdict(
            lambda: deque(maxlen=max_messages)
        )

    def add(self, chat_id: str, role: str, text: str) -> None:
        self._hist[str(chat_id)].append((role, text))

    def context_block(self, chat_id: str) -> str:
        items = list(self._hist.get(str(chat_id), []))
        if not items:
            return ""
        lines = ["[recent conversation — for context, newest last]"]
        for role, text in items:
            who = "user" if role == "user" else "assistant"
            lines.append(f"{who}: {tokens.truncate_output(text, 1500)}")
        return tokens.truncate_output("\n".join(lines), HISTORY_CONTEXT_CHARS)


def _build_agent(args) -> AgentLoop:
    """Wire provider + tools + loop (mirrors prana.__main__ wiring)."""
    key = None
    if args.key:
        key = args.key
    else:
        order = [args.provider] if args.provider else []
        order += [p for p in ("openai", "anthropic", "gemini") if p not in order]
        for prov in order:
            key = config.get_key(prov)
            if key:
                break
    if not key:
        raise ProviderError(
            "no API key found — pass --key, set OPENAI_API_KEY / ANTHROPIC_API_KEY / "
            "GEMINI_API_KEY, or run `python -m prana --save-key`"
        )
    provider_cls = resolve_provider(key, args.provider)
    provider = provider_cls(api_key=key, model=args.model)
    registry = ToolRegistry()
    for tool in builtin_tools(sandbox_root=args.sandbox or os.getcwd()):
        registry.register(tool)
    for tool in builtin_memory_tools():
        registry.register(tool)
    identity = load_identity(args.identity)
    return AgentLoop(
        provider, registry, identity=identity, max_iters=args.max_iters, mode=args.mode
    )


def _is_command(text: str, cmd: str) -> bool:
    first = text.strip().split()[0].lower() if text.strip() else ""
    return first == cmd or first == "/" + cmd


def handle_message(
    client: WhatsAppClient, loop: AgentLoop, store: ConversationStore,
    from_number: str, text: str,
) -> bool:
    """Process one inbound WhatsApp message. Returns True when a reply was sent."""
    if not text or not text.strip():
        return False

    # Pairing gate: only the owner's messages are processed. The first-ever
    # sender becomes the owner; strangers are silently ignored.
    verdict = auth_mod.check_sender("whatsapp", from_number)
    if verdict == "owner":
        client.send_message(from_number, auth_mod.PAIRING_NOTICE)
    elif verdict == "denied":
        auth_mod.ignore_unpaired("whatsapp")
        return False

    # Local commands — no LLM call, instant and token-free.
    if _is_command(text, "start"):
        client.send_message(from_number, INTRO)
        return True
    if _is_command(text, "help"):
        client.send_message(from_number, HELP)
        return True

    context = store.context_block(from_number)
    prompt = f"{context}\n\n[current message]\n{text}" if context else text
    store.add(from_number, "user", text)

    event = client.message_to_event(from_number, prompt)
    result = loop.run(event)
    reply = result.final_text or "(no output)"
    client.send_message(from_number, reply)
    store.add(from_number, "assistant", reply)
    return True


def make_handler(
    client: WhatsAppClient,
    loop: AgentLoop,
    store: ConversationStore,
    webhook_path: str,
    verify_token: str,
    app_secret: str = "",
) -> type:
    """Build a webhook request-handler class bound to this listener's state."""

    class WhatsAppWebhookHandler(BaseHTTPRequestHandler):
        server_version = "PRANAWhatsApp/0.1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            # The Meta verify token travels in the GET query string — scrub it
            # before it can land in logs.
            msg = fmt % args
            msg = re.sub(r"hub\.verify_token=[^&\s\"]*", "hub.verify_token=<redacted>", msg)
            sys.stderr.write(f"whatsapp: {msg}\n")

        def _path_parts(self) -> Tuple[str, Dict[str, str]]:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            flat = {k: v[0] for k, v in qs.items() if v}
            return parsed.path, flat

        def _send(self, code: int, body: bytes, ctype: str = "text/plain") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            path, qs = self._path_parts()
            if path.rstrip("/") != webhook_path.rstrip("/"):
                self._send(404, b"not found")
                return
            challenge = verify_webhook(qs, verify_token)
            if challenge is None:
                self._send(403, b"verification failed")
                return
            self.log_message("webhook verified")
            self._send(200, challenge.encode("utf-8"))

        def do_POST(self) -> None:  # noqa: N802 (http.server naming)
            path, _ = self._path_parts()
            if path.rstrip("/") != webhook_path.rstrip("/"):
                self._send(404, b"not found")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            # Cap request size: Meta webhook payloads are KBs; an unbounded
            # read lets anyone with the tunnel URL exhaust memory.
            if length > 1_048_576:
                self._send(413, b"payload too large")
                return
            raw = self.rfile.read(length) if length > 0 else b""
            # Meta signs every POST with the app secret. Without a configured
            # secret anyone who learns the tunnel URL could inject messages.
            if app_secret:
                sig = self.headers.get("X-Hub-Signature-256") or ""
                if not verify_signature(raw, sig, app_secret):
                    self._send(403, b"bad signature")
                    return
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send(400, b"bad json")
                return
            # Always 200 — Meta retries on anything else, which would
            # double-process messages. Agent failures are logged, not retried.
            for from_number, text in parse_webhook(payload):
                try:
                    handle_message(client, loop, store, from_number, text)
                except WhatsAppError as e:
                    self.log_message("send error: %s", e)
                except Exception as e:  # one bad message never kills the listener
                    self.log_message(
                        "message handling failed: %s", type(e).__name__
                    )
            self._send(200, b"OK")

    return WhatsAppWebhookHandler


def serve(
    client: WhatsAppClient,
    loop: AgentLoop,
    port: int = DEFAULT_PORT,
    webhook_path: str = DEFAULT_WEBHOOK_PATH,
    verify_token: str = "",
    store: Optional[ConversationStore] = None,
    ready: Optional[Callable[[int], None]] = None,
    app_secret: str = "",
) -> None:
    """Run the webhook listener until Ctrl+C. ``ready`` gets the bound port."""
    handler = make_handler(
        client, loop, store or ConversationStore(), webhook_path, verify_token,
        app_secret=app_secret,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    bound_port = server.server_address[1]
    print(
        f"💬 PRANA listening for WhatsApp on 127.0.0.1:{bound_port}{webhook_path} "
        "— Ctrl+C to stop",
        file=sys.stderr,
    )
    if ready:
        ready(bound_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping WhatsApp listener", file=sys.stderr)
    finally:
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prana-whatsapp",
        description="PRANA WhatsApp channel — two-way messaging via the "
        "Meta Cloud API webhook.",
    )
    p.add_argument("--key", help="LLM API key (else env / ~/.prana/config.json)")
    p.add_argument(
        "--provider",
        choices=["openai", "anthropic", "gemini"],
        help="LLM provider (auto-detected from key if omitted)",
    )
    p.add_argument("--model", help="model name (provider default if omitted)")
    p.add_argument(
        "--max-iters",
        type=int,
        default=DEFAULT_MAX_ITERS,
        help=f"max ReAct iterations per message (default {DEFAULT_MAX_ITERS})",
    )
    p.add_argument("--identity", help="path to SOUL.md-style identity file")
    p.add_argument(
        "--mode",
        choices=sorted(MODES),
        help="personality mode (default from identity file)",
    )
    p.add_argument(
        "--sandbox", help="sandbox root for file/shell tools (default: cwd)"
    )
    p.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"local webhook listener port (default {DEFAULT_PORT})",
    )
    p.add_argument(
        "--webhook-path",
        default=DEFAULT_WEBHOOK_PATH,
        help=f"webhook URL path (default {DEFAULT_WEBHOOK_PATH})",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        token = resolve_whatsapp_token()
        phone_number_id = resolve_phone_number_id()
    except WhatsAppError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    verify_token = resolve_verify_token()
    if not verify_token:
        print(
            "warning: no webhook verify token set (WHATSAPP_VERIFY_TOKEN) — "
            "Meta webhook verification will reject everything",
            file=sys.stderr,
        )
    app_secret = resolve_app_secret()
    if not app_secret:
        print(
            "warning: webhook signature verification disabled — "
            "set WHATSAPP_APP_SECRET (Meta app → Settings → Basic) so only "
            "Meta can POST to your webhook",
            file=sys.stderr,
        )
    try:
        loop = _build_agent(args)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    client = WhatsAppClient(token, phone_number_id)
    print(
        "Next: expose this listener publicly (ngrok/cloudflared) and set the "
        "Meta app webhook Callback URL to "
        f"https://<your-tunnel>{args.webhook_path} (subscribe: messages).",
        file=sys.stderr,
    )
    serve(
        client,
        loop,
        port=args.port,
        webhook_path=args.webhook_path,
        verify_token=verify_token,
        app_secret=app_secret,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
