"""Intent router — the fast, cheap tier of the harness.

Before the main model does any work, a small model (Haiku) reads the utterance
and decides what it *means*: a control command Neovis handles itself (change
voice, stop) or a task for the gated agent. This is deliberately model-driven,
not a pile of regexes — Haiku understands "make it sound posh" (→ British),
"talk like a guy" (→ male), "cut it out" (→ stop) without us enumerating phrasings.

It degrades gracefully: if no model is reachable, it falls back to a rule-based
parse so the desktop voice loop still works offline and in tests.
"""

from __future__ import annotations

import json
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

ROUTER_MODEL = "claude-haiku-4-5"  # small + fast; the main agent runs the big model

_SYSTEM = """\
You are Neovis's fast intent router. Read the user's LATEST message and output
ONLY a compact JSON object — no prose, no code fence.

Choose "action":
- "voice" — they want to change the assistant's SPEAKING VOICE (accent/gender),
  in ANY phrasing. Also fill, using null when unclear:
    "accent": "american" | "british" | null   (e.g. "posh"/"UK" → british)
    "gender": "male" | "female" | null         (e.g. "a guy" → male, "a lady" → female)
    "name":   "sky" | "adam" | "emma" | "george" | null  (only if named)
- "stop" — they want to stop / cancel / abort the current task.
- "chat" — a greeting, thanks, small talk, OR a general question you can fully
  answer yourself in one short sentence WITHOUT touching the computer (what can
  you do, how are you, banter). Also fill:
    "reply": a short, warm, JARVIS-butler-style response (max 20 words, no markdown)
  NOT chat if it needs the machine (files, apps, screen, status of anything),
  or mixes in a request ("hey, open chrome") — those are "task".
- "task" — anything else: a request for the agent to DO something on the computer.

If the message is preceded by "[note: a task is currently running]", the user
may just be checking progress: classify pure status questions ("how's it
going", "you done yet?", "still there?") as "chat" with a short progress-toned
reply ("Still on it — nearly there."). A NEW or CHANGED request stays "task".

Only classify the CURRENT message; ignore earlier ones. Examples:
"make it sound british"        -> {"action":"voice","accent":"british","gender":null,"name":null}
"talk like a deep-voiced guy"  -> {"action":"voice","accent":null,"gender":"male","name":null}
"switch to emma"               -> {"action":"voice","accent":null,"gender":null,"name":"emma"}
"cut it out"                    -> {"action":"stop"}
"hey"                           -> {"action":"chat","reply":"Hey! What can I do for you?"}
"thanks, that's perfect"        -> {"action":"chat","reply":"Anytime."}
"email my boss the q2 report"  -> {"action":"task"}"""


class IntentRouter:
    def __init__(self, model: str = ROUTER_MODEL):
        self._client = ClaudeSDKClient(
            options=ClaudeAgentOptions(
                model=model,
                system_prompt=_SYSTEM,
                allowed_tools=[],
                setting_sources=[],
                env=dict(os.environ),
            )
        )
        self.available = False
        self._lock = None  # created lazily on the running loop

    async def connect(self) -> None:
        try:
            await self._client.connect()
            self.available = True
        except Exception:
            self.available = False

    async def disconnect(self) -> None:
        if self.available:
            try:
                await self._client.disconnect()
            except Exception:
                pass

    async def classify(self, text: str, *, busy: bool = False) -> dict:
        # Tier 0: instant rules for unambiguous commands (skip the model entirely).
        fast = _fast_rules(text)
        if fast is not None:
            return fast
        if not self.available:
            return rule_fallback(text)
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        query = f"[note: a task is currently running]\n{text}" if busy else text
        try:
            # One classification at a time — concurrent queries would interleave
            # turns on the single underlying client.
            async with self._lock:
                await self._client.query(query)
                parts: list[str] = []
                async for msg in self._client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                parts.append(block.text)
                    elif isinstance(msg, ResultMessage):
                        break
            raw = "".join(parts)
            i, j = raw.find("{"), raw.rfind("}")
            if i >= 0 and j > i:
                data = json.loads(raw[i : j + 1])
                if isinstance(data, dict) and data.get("action") in ("voice", "stop", "task", "chat"):
                    return data
            return {"action": "task"}
        except Exception:
            return rule_fallback(text)


_STOP_WORDS = ("stop", "/stop", "cancel", "abort", "nevermind", "never mind", "cut it out")


def _fast_rules(text: str) -> dict | None:
    """Tier 0: return a confident classification for obvious commands, else None
    (→ escalate to the model). Only short-circuits when we're sure."""
    from ..voice.commands import parse_voice_command

    if text.strip().lower() in _STOP_WORDS:
        return {"action": "stop"}
    intent = parse_voice_command(text)
    if intent is not None and intent.specified:  # an explicit voice request
        return {"action": "voice", "accent": intent.accent, "gender": intent.gender, "name": intent.name}
    return None


def rule_fallback(text: str) -> dict:
    """Offline classifier (no model): fast rules, else assume it's a task."""
    return _fast_rules(text) or {"action": "task"}
