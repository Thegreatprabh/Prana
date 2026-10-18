"""Built-in tools for PRANA P1."""

from .base import SideEffect, Tool, ToolCall, ToolResult, tool_names, validate_args
from .files import builtin_file_tools
from .registry import Approver, ToolRegistry, console_approver
from .schedule_tool import builtin_schedule_tools
from .shell import builtin_shell_tools
from .web import builtin_web_tools

__all__ = [
    "SideEffect",
    "Tool",
    "ToolCall",
    "ToolResult",
    "ToolRegistry",
    "Approver",
    "console_approver",
    "builtin_file_tools",
    "builtin_shell_tools",
    "builtin_web_tools",
    "builtin_schedule_tools",
    "tool_names",
    "validate_args",
]


def builtin_tools(sandbox_root: str | None = None) -> list[Tool]:
    """All built-in tools (P1 + P3 schedule). `sandbox_root` confines file/shell tools."""
    tools: list[Tool] = []
    tools.extend(builtin_shell_tools(sandbox_root))
    tools.extend(builtin_file_tools(sandbox_root))
    tools.extend(builtin_web_tools())
    tools.extend(builtin_schedule_tools())
    return tools
