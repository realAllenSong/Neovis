"""Block Kit builders for the Slack channel.

The approval card is the visible half of the security story: a dangerous action
pauses on your phone with the exact tool + args and an Approve / Deny button.
"""

from __future__ import annotations

import re
from typing import Any

_RISK_EMOJI = {"SAFE": "🟢", "MODERATE": "🟡", "DANGEROUS": "🔴"}


def to_mrkdwn(text: str) -> str:
    """Convert the model's Markdown to Slack 'mrkdwn' (which is *not* Markdown)."""
    t = text or ""
    # [label](url) -> <url|label>
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", t)
    # ATX headers -> bold line
    t = re.sub(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*$", r"*\1*", t, flags=re.M)
    # **bold** / __bold__ -> *bold*
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t)
    t = re.sub(r"__(.+?)__", r"*\1*", t)
    # - / * bullets -> •
    t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.M)
    return t.strip()


def _fmt_args(args: dict[str, Any]) -> str:
    if not args:
        return "_(no arguments)_"
    parts = []
    for k, v in list(args.items())[:6]:
        text = str(v)
        if len(text) > 300:
            text = text[:300] + "…"
        parts.append(f"• *{k}*: `{text}`")
    return "\n".join(parts)


def approval_blocks(request_id: str, tool: str, args: dict, risk: str) -> list[dict]:
    """Interactive approval card. Button values carry the request id."""
    emoji = _RISK_EMOJI.get(risk, "🔴")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *Approval needed* — `{tool}` ({risk})",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _fmt_args(args)}},
        {
            "type": "actions",
            "block_id": f"approval:{request_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "neovis_approve",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "action_id": "neovis_deny",
                    "value": request_id,
                },
            ],
        },
    ]


def decided_blocks(tool: str, risk: str, approved: bool, approver: str) -> list[dict]:
    """Replaces the buttons once a decision is made, so it can't be double-clicked."""
    verb = "✅ Approved" if approved else "⛔ Denied"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{verb} `{tool}` ({risk}) by <@{approver}>",
            },
        }
    ]
