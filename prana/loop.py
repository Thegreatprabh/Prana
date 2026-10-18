"""The ReAct core loop — the heart of PRANA (SPEC.md §2.1).

One event in → reason → act (tool calls) → observe → repeat → final answer.
Model-agnostic: the loop speaks the normalized Provider interface and never
touches vendor-specific payloads.

Budgets (all enforced):
- max_iters: cap on model↔tool round trips (default 25)
- wall_clock_s: cap on total seconds (default 600)
- interrupt: KeyboardInterrupt stops gracefully and returns partial progress

Every tool observation is truncated (~4000 chars) before re-entering the
context, and history is compacted when it grows past budget.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import personality, tokens
from .events import Event
from .memory import recall as mem_recall
from .memory.recall import MAX_RECALL_CHARS
from .providers.base import Completion, Provider
from .tools.registry import ToolRegistry

DEFAULT_MAX_ITERS = 25
DEFAULT_WALL_CLOCK_S = 600
HISTORY_COMPACT_TOKENS = 12000


@dataclass
class RunResult:
    final_text: str
    iterations: int
    tool_calls_made: int
    stop_reason: str  # "done" | "max_iters" | "wall_clock" | "interrupted" | "provider_error"
    elapsed_s: float = 0.0
    observations: List[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        identity: Optional[str] = None,
        max_iters: int = DEFAULT_MAX_ITERS,
        wall_clock_s: int = DEFAULT_WALL_CLOCK_S,
        mode: Optional[str] = None,
        use_memory: bool = True,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.identity = identity if identity is not None else personality.load_identity()
        self.max_iters = max(1, max_iters)
        self.wall_clock_s = max(1, wall_clock_s)
        self.mode = mode
        self.use_memory = use_memory
        self._call_seq = itertools.count(1)

    def _recalled_memories(self, event: Event) -> str:
        """Relevant memories for this event, token-capped. Never raises.

        Memory is an enhancement, not load-bearing: any failure degrades to
        no-memory rather than breaking the run. Empty store → empty string
        (the agent must never invent memories).
        """
        if not self.use_memory:
            return ""
        try:
            hits = mem_recall.recall(event.text, k=5)
        except Exception:
            return ""
        if not hits:
            return ""
        return tokens.truncate_output("\n".join(hits), MAX_RECALL_CHARS)

    def _system_prompt(self, event: Event) -> str:
        mode = personality.resolve_mode(self.mode, self.identity)
        return personality.assemble_system_prompt(
            self.identity,
            self.registry.all(),
            event,
            mode=mode,
            memory_summary=self._recalled_memories(event),
        )

    def run(self, event: Event) -> RunResult:
        """Run one event to completion (or budget). Never raises on provider error."""
        start = time.monotonic()
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self._system_prompt(event)},
            {"role": "user", "content": event.text},
        ]
        iterations = 0
        tool_calls_made = 0
        observations: List[str] = []
        stop_reason = "done"
        final_text = ""

        try:
            while iterations < self.max_iters:
                if time.monotonic() - start > self.wall_clock_s:
                    stop_reason = "wall_clock"
                    break
                iterations += 1

                messages = tokens.compact_history(messages, HISTORY_COMPACT_TOKENS)
                try:
                    completion: Completion = self.provider.complete(
                        messages, self.registry.specs()
                    )
                except Exception as e:
                    # Provider errors end the run gracefully. Scrub key-like
                    # strings defense-in-depth (providers redact first).
                    final_text = f"Provider error: {tokens.scrub_secrets(str(e))}"
                    stop_reason = "provider_error"
                    break

                if not completion.tool_calls:
                    final_text = completion.text or "(no output)"
                    stop_reason = "done"
                    break

                # Execute each requested tool, feed truncated observations back.
                for call in completion.tool_calls:
                    if not call.id:
                        call.id = f"call-{next(self._call_seq)}"
                    result = self.registry.dispatch(call)
                    tool_calls_made += 1
                    obs = tokens.truncate_output(result.observation())
                    observations.append(obs)
                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"[tool call: {call.name} {call.arguments}]",
                        }
                    )
                    messages.append({"role": "user", "content": f"[tool result]\n{obs}"})
            else:
                stop_reason = "max_iters"
        except KeyboardInterrupt:
            stop_reason = "interrupted"
            final_text = final_text or "(interrupted by user — partial progress kept above)"

        elapsed = time.monotonic() - start
        return RunResult(
            final_text=final_text,
            iterations=iterations,
            tool_calls_made=tool_calls_made,
            stop_reason=stop_reason,
            elapsed_s=round(elapsed, 2),
            observations=observations,
        )
