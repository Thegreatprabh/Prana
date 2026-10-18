"""Tool primitives: the Tool dataclass, side-effect classes, call/result types.

Every tool declares its side effects up front. The registry's approval gate
(SPEC.md §2.2, §5) uses this classification: `read` runs freely, `write`
and `external` run with logging, `irreversible` ALWAYS requires explicit
user approval — no config flag may silently disable that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SideEffect(str, Enum):
    READ = "read"  # no state change (read file, list dir)
    WRITE = "write"  # local state change (write/edit file, run command)
    EXTERNAL = "external"  # leaves the machine (web search, fetch)
    IRREVERSIBLE = "irreversible"  # cannot be undone (send, delete, publish, pay)


@dataclass
class Tool:
    name: str
    description: str
    # JSON-schema-style parameters: {"type": "object", "properties": {...}, "required": [...]}
    parameters: Dict[str, Any]
    side_effect: SideEffect
    func: Callable[..., str]

    def spec(self) -> Dict[str, Any]:
        """Provider-agnostic tool spec (providers translate to their own format)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class ToolCall:
    """One model-requested tool invocation (provider-agnostic)."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Outcome of one tool invocation."""

    call_id: str
    name: str
    output: str
    is_error: bool = False

    def observation(self) -> str:
        prefix = "ERROR: " if self.is_error else ""
        return f"{prefix}[{self.name}]\n{self.output}"


def validate_args(tool: Tool, args: Dict[str, Any]) -> Optional[str]:
    """Lightweight validation against the tool's parameter schema.

    Returns an error message string, or None when args look valid.
    This is a UX guard, not a security boundary — tools must validate
    their own inputs again where it matters.
    """
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    schema = tool.parameters or {}
    props = schema.get("properties", {})
    required = schema.get("required", [])
    for req in required:
        if req not in args:
            return f"missing required argument: {req}"
    for key in args:
        if key not in props:
            return f"unknown argument: {key}"
    return None


def tool_names(tools: List[Tool]) -> List[str]:
    return [t.name for t in tools]
