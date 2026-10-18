"""Tool registry + approval gate.

The approval gate is the load-bearing safety mechanism (SPEC.md §5.1):
irreversible tools ALWAYS ask the user, every time, with no silent bypass.
The gate is enforced here in `dispatch`, not left to the model or to
per-tool goodwill.
"""

from __future__ import annotations

import sys
from typing import Callable, Dict, List, Optional

from .base import SideEffect, Tool, ToolCall, ToolResult, validate_args

#: Approver answers "may this irreversible tool call proceed?"
Approver = Callable[[str, Dict], bool]


def console_approver(tool_name: str, args: Dict) -> bool:
    """Default approver: asks on the terminal. y/yes proceeds, anything else denies."""
    print(f"\n⚠️  PRANA wants to run IRREVERSIBLE tool '{tool_name}'", file=sys.stderr)
    print(f"   args: {args}", file=sys.stderr)
    try:
        answer = input("   Approve? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n   denied (no input)", file=sys.stderr)
        return False
    return answer in ("y", "yes")


class ToolRegistry:
    def __init__(self, approver: Optional[Approver] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        # Default approver is the console prompt. There is intentionally no
        # "auto_approve" flag: irreversible actions must never be silently enabled.
        self.approver: Approver = approver or console_approver

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all(self) -> List[Tool]:
        return list(self._tools.values())

    def specs(self) -> List[Dict]:
        return [t.spec() for t in self._tools.values()]

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Validate → approval gate → execute → result. Never raises."""
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"unknown tool: {call.name}", is_error=True)

        err = validate_args(tool, call.arguments)
        if err:
            return ToolResult(call.id, call.name, f"invalid arguments: {err}", is_error=True)

        # ---- approval gate (SPEC.md §5.1) ----
        if tool.side_effect == SideEffect.IRREVERSIBLE:
            try:
                approved = self.approver(tool.name, call.arguments)
            except Exception as e:  # a broken approver must fail closed
                return ToolResult(
                    call.id, call.name,
                    f"approver error — denied by default: {e}", is_error=True,
                )
            if not approved:
                return ToolResult(
                    call.id, call.name,
                    "denied by user: irreversible action requires explicit approval",
                    is_error=True,
                )

        # ---- execute ----
        try:
            output = tool.func(**call.arguments)
            if not isinstance(output, str):
                output = str(output)
            return ToolResult(call.id, call.name, output)
        except Exception as e:
            # Never leak tracebacks with potential secrets; keep the message short.
            return ToolResult(call.id, call.name, f"tool raised {type(e).__name__}: {e}", is_error=True)
