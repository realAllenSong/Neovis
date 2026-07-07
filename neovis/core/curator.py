"""Background memory curation — the self-learning half of the loop.

After every completed turn, the session fires the turn digest (user message +
final reply) at a persistent small-model reviewer that decides on its own
whether anything durable should be written to Neovis's memory — and writes it
with the ``memory`` tool. The main conversation is never blocked or touched.

This adapts Hermes Agent's ``background_review`` fork: Hermes replays the full
transcript into a fork of the main agent to reuse its warm prompt cache; when
routed to a *different* model it sends a compact digest instead, because a
different model can't share the cache anyway. We always route to Haiku (cheap,
fast), so the digest mode is the right shape. Tool whitelist = memory only.

Fire-and-forget with a busy-drop: if a review is still running when the next
turn ends, the new digest is dropped — memory curation is best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from .memory import MemoryStore, build_memory_mcp

logger = logging.getLogger(__name__)

CURATOR_MODEL = "claude-haiku-4-5"

_SYSTEM = """\
You are Neovis's background memory curator. You receive one finished exchange
between the user and Neovis, plus Neovis's current memory. Decide whether the
exchange contains a DURABLE fact worth remembering across sessions:
- people and their roles/contact info ("the CTO is Alice, alice@fund.com")
- stable paths, project conventions, environment quirks
- the user's preferences, habits, corrections ("always answer in Chinese")

Most exchanges contain NOTHING durable — then reply exactly: nothing
If there is something, write it with the memory tool (target "memory" for
machine/project/people facts, "user" for the user themself), keeping entries
short; UPDATE an existing entry (replace) rather than duplicating it. Never
store secrets, passwords, tokens, or one-off task details. Then reply: saved
"""

# Hard-block the general-purpose tools; the memory MCP tool is all it needs.
_DISALLOWED = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch",
               "WebFetch", "Task", "NotebookEdit", "TodoWrite"]


class MemoryCurator:
    """A persistent Haiku client that only knows how to curate memory."""

    def __init__(self, model: str = CURATOR_MODEL):
        self._client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                model=model,
                system_prompt=_SYSTEM,
                mcp_servers={"neovis-memory": build_memory_mcp(MemoryStore())},
                allowed_tools=["mcp__neovis-memory__memory"],
                disallowed_tools=_DISALLOWED,
                setting_sources=[],
                env=dict(os.environ),
            )
        )
        self.available = False
        self._busy = False

    async def connect(self) -> None:
        try:
            await self._client.connect()
            self.available = True
        except Exception:
            self.available = False

    async def disconnect(self) -> None:
        if self.available:
            self.available = False
            try:
                await self._client.disconnect()
            except Exception:
                pass

    def review_later(self, user_msg: str, reply: str) -> None:
        """Fire-and-forget: schedule a review of this turn on the running loop."""
        if not self.available or self._busy:
            return
        task = asyncio.create_task(self._review(user_msg, reply))
        task.add_done_callback(lambda t: t.exception())  # swallow, but retrieve

    async def _review(self, user_msg: str, reply: str) -> None:
        self._busy = True
        try:
            digest = (
                f"User: {user_msg[:1200]}\n"
                f"Neovis: {reply[:1200]}\n\n"
                f"Current memory:\n{MemoryStore().snapshot() or '(empty)'}\n\n"
                "Anything durable to save or update? Remember: usually 'nothing'."
            )
            await self._client.query(digest)
            async for msg in self._client.receive_response():
                if isinstance(msg, ResultMessage):
                    break
                if isinstance(msg, AssistantMessage):
                    pass  # tool use (memory writes) happens inside the engine
        except Exception as exc:
            logger.debug("memory curation failed: %s", exc)
        finally:
            self._busy = False
