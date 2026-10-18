"""Personality core (P2): identity loading + modes + system-prompt assembly.

The system prompt is assembled fresh each turn in this order:
identity → mode → memory summary → base rules → tool docs → event context.
Modes/personas beyond the identity file arrive in P2; the assembly seam is already here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from . import config
from .events import Event
from .tools.base import Tool

#: Selectable registers. Same brain, different voice.
MODES = {
    "casual": (
        "Mode: casual. Talk like a sharp friend — warm, direct, a little playful. "
        "Short answers for simple questions; no lectures unless asked."
    ),
    "teacher": (
        "Mode: teacher. Patient and practical. Break complex ideas into steps, "
        "use examples, check understanding before moving on. Never condescend."
    ),
    "engineer": (
        "Mode: engineer. Rigorous and detail-obsessed. Verify before claiming, "
        "show your work, prefer precise numbers over adjectives. Test everything."
    ),
    "strategist": (
        "Mode: strategist. Big-picture and opportunity-minded. Name trade-offs "
        "honestly, think in milestones and risks, keep the long game in view."
    ),
}

DEFAULT_MODE = "casual"

BASE_RULES = """\
Operating rules (non-negotiable):
1. Use the tools to do real work — never claim work you did not do.
2. Verify results with tools before reporting them as done.
3. Irreversible actions (send, delete, publish, pay) need explicit user approval, every time.
4. Never reveal, repeat, or log API keys, tokens, or passwords — yours or anyone's.
5. Be direct and concise. No filler, no corporate speak.
6. If a tool errors, read the error, adjust, and retry differently — don't loop the same failing call.
7. When the task is complete, summarize what was done and what remains.
"""


def load_identity(path: Optional[str] = None) -> str:
    """Load identity markdown. Explicit path > ~/.prana/SOUL.md > default."""
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(config.identity_path())
    for cand in candidates:
        try:
            text = cand.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return config.default_identity()


def resolve_mode(cli_mode: Optional[str] = None, identity_text: str = "") -> str:
    """Pick the active mode: CLI flag > `mode:` line in identity > default.

    The identity file may declare e.g. ``mode: teacher`` on its own line.
    Unknown values fall back to the default rather than erroring.
    """
    if cli_mode and cli_mode in MODES:
        return cli_mode
    m = re.search(r"(?im)^mode:\s*([a-z]+)\s*$", identity_text or "")
    if m and m.group(1) in MODES:
        return m.group(1)
    return DEFAULT_MODE


def mode_block(mode: str) -> str:
    """The register instruction for a mode."""
    return MODES.get(mode, MODES[DEFAULT_MODE])


def _tool_docs(tools: List[Tool]) -> str:
    lines = ["Available tools (call them as JSON; you never act without tools):"]
    for t in tools:
        props = (t.parameters or {}).get("properties", {})
        arg_names = ", ".join(sorted(props)) or "none"
        lines.append(f"- {t.name}({arg_names}) [{t.side_effect.value}]: {t.description}")
    return "\n".join(lines)


def assemble_system_prompt(
    identity: str,
    tools: List[Tool],
    event: Event,
    extra_context: str = "",
    mode: str = DEFAULT_MODE,
    memory_summary: str = "",
) -> str:
    """Compose the full system prompt for one event.

    Order: identity → mode → memory summary → base rules → tool docs →
    event context → extra context.
    """
    parts = [identity.strip(), "", mode_block(mode).strip()]
    if memory_summary.strip():
        parts += ["", "What you remember (from long-term memory — do not invent beyond this):",
                  memory_summary.strip()]
    parts += ["", BASE_RULES.strip(), "", _tool_docs(tools)]
    ctx = event.context_line()
    if ctx:
        parts += ["", f"Current event: {ctx}"]
    if extra_context.strip():
        parts += ["", extra_context.strip()]
    parts += [
        "",
        "When you need a tool, emit a tool call. When the task is done, reply with the result.",
    ]
    return "\n".join(parts)
