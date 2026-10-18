"""PRANA CLI.

    python -m prana "summarize this repo"      # one-shot task
    python -m prana --chat                     # interactive REPL
    python -m prana --save-key                 # store API key (0600) once

Key resolution: --key flag > environment > ~/.prana/config.json.
Provider auto-detected from the key prefix unless --provider is given.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import __version__, config, events
from .loop import AgentLoop, DEFAULT_MAX_ITERS
from .personality import MODES, load_identity
from .providers.base import ProviderError
from .providers.detect import resolve_provider
from .tools import ToolRegistry, builtin_tools
from .tools.memory_tools import builtin_memory_tools


def resolve_key(args) -> str:
    """Key precedence: --key > env > config file. Clear error when missing."""
    if args.key:
        return args.key
    provider_hint = (args.provider or "").lower() or None
    # Try env/config for the hinted provider first, then any provider.
    order = [provider_hint] if provider_hint else []
    order += [p for p in ("openai", "anthropic", "gemini") if p not in order]
    for prov in order:
        key = config.get_key(prov)
        if key:
            return key
    raise ProviderError(
        "no API key found — pass --key, set OPENAI_API_KEY / ANTHROPIC_API_KEY / "
        "GEMINI_API_KEY, or run `python -m prana --save-key`"
    )


def build_agent(args):
    key = resolve_key(args)
    provider_cls = resolve_provider(key, args.provider)
    provider = provider_cls(api_key=key, model=args.model)
    registry = ToolRegistry()
    for tool in builtin_tools(sandbox_root=args.sandbox or os.getcwd()):
        registry.register(tool)
    for tool in builtin_memory_tools():
        registry.register(tool)
    identity = load_identity(args.identity)
    loop = AgentLoop(
        provider, registry, identity=identity, max_iters=args.max_iters,
        mode=args.mode,
    )
    return loop, provider


def cmd_save_key(_args) -> int:
    print("Which provider? (openai/anthropic/gemini)")
    provider = input("> ").strip().lower()
    if provider not in ("openai", "anthropic", "gemini"):
        print("unknown provider", file=sys.stderr)
        return 2
    key = getpass.getpass("API key (hidden): ").strip()
    if not key:
        print("empty key — not saved", file=sys.stderr)
        return 2
    path = config.set_key(provider, key)
    print(f"saved to {path} (0600)")
    return 0


def cmd_once(args) -> int:
    try:
        loop, provider = build_agent(args)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"🧠 PRANA v{__version__} · {provider.name} · {provider.model}", file=sys.stderr)
    event = events.user_message(args.task)
    try:
        result = loop.run(event)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    print(result.final_text)
    print(
        f"\n— done in {result.iterations} iteration(s), "
        f"{result.tool_calls_made} tool call(s), {result.elapsed_s}s [{result.stop_reason}]",
        file=sys.stderr,
    )
    return 0


def cmd_chat(args) -> int:
    try:
        loop, provider = build_agent(args)
    except ProviderError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"🧠 PRANA v{__version__} · {provider.name} · {provider.model} — /quit to exit",
          file=sys.stderr)
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!", file=sys.stderr)
            break
        if not text:
            continue
        if text in ("/quit", "/exit", "quit", "exit"):
            print("bye!", file=sys.stderr)
            break
        result = loop.run(events.user_message(text))
        print(f"\nprana> {result.final_text}")
        print(f"[{result.iterations} iters · {result.tool_calls_made} tools · "
              f"{result.elapsed_s}s · {result.stop_reason}]", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prana",
        description="PRANA — give any chatbot a life. Autonomous agent CLI (P1).",
    )
    p.add_argument("task", nargs="?", help="one-shot task to complete")
    p.add_argument("--chat", action="store_true", help="interactive chat mode")
    p.add_argument("--key", help="API key (else env / ~/.prana/config.json)")
    p.add_argument("--provider", choices=["openai", "anthropic", "gemini"],
                   help="provider (auto-detected from key if omitted)")
    p.add_argument("--model", help="model name (provider default if omitted)")
    p.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS,
                   help=f"max ReAct iterations (default {DEFAULT_MAX_ITERS})")
    p.add_argument("--identity", help="path to SOUL.md-style identity file")
    p.add_argument("--mode", choices=sorted(MODES),
                   help=f"personality mode (default: {sorted(MODES)[0]}; "
                        "identity file 'mode:' line also works)")
    p.add_argument("--sandbox", help="sandbox root for file/shell tools (default: cwd)")
    p.add_argument("--save-key", action="store_true",
                   help="securely save an API key to ~/.prana/config.json")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.save_key:
        return cmd_save_key(args)
    if args.chat:
        return cmd_chat(args)
    if not args.task:
        build_parser().print_help()
        return 2
    return cmd_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
