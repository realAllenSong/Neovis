"""Neovis self-control — the agent's handle on its own permission mode.

The user says "keep auto mode on until I say otherwise" (voice or Slack); the
agent calls the ``auto_mode`` tool. Turning it ON is itself gated as a
LOCAL_WRITE — one approval card, and from then on local writes run unprompted
until the user turns it off (free). OUTWARD actions (send/submit/delete/push)
STILL always confirm — pinning never weakens that, and browser clicks are
never auto-approved either.

Without this tool the model invents Claude-Code-isms (editing
~/.claude/settings.json, suggesting /config) that have zero effect on Neovis's
gate — the gate is the only permission authority here.
"""

from __future__ import annotations

from .gate import AutoMode

_SCHEMA = {
    "type": "object",
    "properties": {
        "enable": {"type": "boolean",
                   "description": "true = keep auto-approval ON until turned off; false = back to confirming"},
    },
    "required": ["enable"],
}


def build_control_mcp(automode: AutoMode):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "auto_mode",
        "Pin or unpin auto-approval of LOCAL actions (file writes, shell "
        "changes) across tasks, per the user's explicit request. Outward or "
        "irreversible actions (sending, submitting, deleting, pushing) always "
        "still require confirmation. Use when the user says things like 'auto "
        "approve everything from now on' / 'stop auto mode'.",
        _SCHEMA,
    )
    async def auto_mode(args: dict) -> dict:
        import json

        if bool(args.get("enable")):
            automode.pin()
            msg = ("Auto-approval pinned ON: local actions run without prompts "
                   "until the user turns it off. Outward/irreversible actions "
                   "still require confirmation.")
        else:
            automode.unpin()
            msg = "Auto-approval OFF: every write asks again."
        return {"content": [{"type": "text", "text": json.dumps({"ok": True, "status": msg})}]}

    return create_sdk_mcp_server(name="neovis-control", tools=[auto_mode])
