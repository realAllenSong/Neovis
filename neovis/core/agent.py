"""The agent loop.

Assembles a model-agnostic pydantic-ai Agent from config + the tool registry,
and wraps it in a :class:`Session` that keeps conversation history so a user can
say "now do that in the other folder" and be understood.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass, field

from pydantic_ai import Agent

from .config import AppConfig
from .deps import NeovisDeps
from .llm import build_model
from .registry import REGISTRY, ToolRegistry

SYSTEM_PROMPT = """\
You are Neovis, a JARVIS-style operator embedded on a colleague's work computer
at a hedge fund. You carry out requests by calling tools that act on THIS
machine — inspecting the screen, reading and organising files, running commands,
watching long jobs.

Operating principles:
- Be decisive and concrete. Prefer doing the task over describing it.
- Dangerous actions (shell, deletes, GUI control) are gated: when you call such
  a tool the human is asked to approve. If a call comes back REJECTED or DENIED,
  stop and explain — do not try to route around the gate.
- Never fabricate results. Report exactly what a tool returned.
- Keep spoken/chat replies short; this user is often on their phone.
- When a task finishes, state the outcome and where any output landed.

Host: {host} ({system}).
"""


def build_agent(
    config: AppConfig,
    registry: ToolRegistry | None = None,
    model=None,
) -> Agent[NeovisDeps, str]:
    """Build the agent. Pass ``model`` (e.g. a TestModel) to override config."""
    reg = registry or REGISTRY
    return Agent(
        model or build_model(config.llm),
        deps_type=NeovisDeps,
        tools=reg.build_tools(),
        system_prompt=SYSTEM_PROMPT.format(
            host=config.host_label,
            system=f"{platform.system()} {platform.release()}",
        ),
    )


@dataclass
class Session:
    """A single conversation: the agent, its deps, and rolling message history."""

    agent: Agent[NeovisDeps, str]
    deps: NeovisDeps
    history: list = field(default_factory=list)

    async def send(self, message: str) -> str:
        """Run one turn, threading prior history so context carries over."""
        self.deps.cancel.reset()
        result = await self.agent.run(
            message, deps=self.deps, message_history=self.history
        )
        self.history = result.all_messages()
        return result.output

    def stop(self) -> None:
        """Signal cooperative cancellation for the in-flight turn."""
        self.deps.cancel.set()
