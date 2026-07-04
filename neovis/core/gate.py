"""The permission gate — Neovis's consequence-gated `can_use_tool` callback.

The Claude Agent SDK calls this before every tool use. We classify the action by
*consequence*, not by tool, and apply the user's policy:

* READ          → allow after an allowlist check (no interruption)
* LOCAL_WRITE   → approve once; "approve + auto" enters auto-mode for the series
* OUTWARD       → always ask a human, even in auto-mode; severe ones double-confirm

A denylisted shell command is a hard deny that approval cannot override.

"Asking a human" is done by awaiting an :class:`ApprovalGateway` (Slack buttons on
the phone, or the console) — the callback simply blocks until the gateway returns.
Every decision is written to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from .approval import ApprovalGateway, ApprovalRequest
from .audit import AuditLog, AuditRecord
from .policy import PolicyConfig


class Consequence(IntEnum):
    READ = 0          # observe only — screen, files, pages
    LOCAL_WRITE = 1   # change local state — write a file, click a link, run a script
    OUTWARD = 2       # leave the machine / irreversible — send, submit, delete, push


# Built-in Claude Code tools whose consequence is fixed by name.
_READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "WebSearch", "TodoWrite"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Browser MCP actions (e.g. "mcp__chrome-devtools__click" → "click").
_BROWSER_READ = {
    "navigate_page", "new_page", "list_pages", "select_page", "take_snapshot",
    "take_screenshot", "list_console_messages", "get_console_message",
    "list_network_requests", "get_network_request", "wait_for", "hover",
    "evaluate_script",
}
_BROWSER_WRITE = {
    "click", "fill", "fill_form", "type_text", "drag", "press_key",
    "upload_file", "resize_page", "emulate",
}


def _action(tool_name: str) -> str:
    """'mcp__chrome-devtools__click' → 'click'; 'Read' → 'Read'."""
    return tool_name.rsplit("__", 1)[-1]


def _text_blob(tool_input: dict[str, Any]) -> str:
    return " ".join(str(v) for v in tool_input.values()).lower()


def classify(tool_name: str, tool_input: dict[str, Any], policy: PolicyConfig) -> tuple[Consequence, str]:
    """Map a tool call to a consequence tier plus a short human-readable reason."""
    if tool_name in _READ_TOOLS:
        return Consequence.READ, "read-only tool"
    if tool_name in _WRITE_TOOLS:
        return Consequence.LOCAL_WRITE, "writes a local file"

    action = _action(tool_name)
    blob = _text_blob(tool_input)
    hits_outward = any(k.lower() in blob for k in policy.outward_keywords)

    if tool_name == "Bash" or action == "bash":
        cmd = str(tool_input.get("command", ""))
        if policy.matches_any(policy.bash_outward_patterns, cmd):
            return Consequence.OUTWARD, "shell command leaves the machine / is irreversible"
        if policy.matches_any(policy.bash_read_patterns, cmd):
            return Consequence.READ, "read-only shell command"
        return Consequence.LOCAL_WRITE, "shell command modifies local state"

    if action in _BROWSER_READ:
        return Consequence.READ, "browser navigation / read"
    if action in _BROWSER_WRITE:
        if hits_outward:
            return Consequence.OUTWARD, "browser action matches an outward keyword (e.g. Send/Submit)"
        return Consequence.LOCAL_WRITE, "browser interaction"

    # Unknown tool: outward if its input smells outward, else treat as a local write.
    if hits_outward:
        return Consequence.OUTWARD, "input matches an outward keyword"
    return Consequence.LOCAL_WRITE, "unclassified tool (defaults to local write)"


@dataclass
class AutoMode:
    """Whether we're inside an approved series of local writes. Reset per task."""

    active: bool = False

    def enable(self) -> None:
        self.active = True

    def reset(self) -> None:
        self.active = False


def _read_target(tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "path", "url", "pattern", "query"):
        if tool_input.get(key):
            return str(tool_input[key])
    return ""


def build_can_use_tool(
    policy: PolicyConfig,
    audit: AuditLog,
    approval: ApprovalGateway,
    automode: AutoMode,
    *,
    actor: str = "user",
    session_id: str = "desktop",
):
    """Build the SDK `can_use_tool` callback bound to this session's deps."""

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any):
        def record(tier: str, status: str, approver: str | None = None, result: str | None = None) -> None:
            audit.record(
                AuditRecord(
                    tool=tool_name, risk=tier, status=status, args=tool_input,
                    session_id=session_id, actor=actor, approver=approver, result=result,
                )
            )

        # 1) Hard deny — a denylisted shell command cannot be approved around.
        if tool_name == "Bash":
            rule = policy.is_shell_denied(str(tool_input.get("command", "")))
            if rule:
                record("DENYLIST", "denied", result=rule)
                return PermissionResultDeny(
                    message=f"blocked by denylist rule /{rule}/", interrupt=False
                )

        consequence, why = classify(tool_name, tool_input, policy)

        # 2) READ — allow after the allowlist check; never interrupt the user.
        if consequence is Consequence.READ:
            target = _read_target(tool_input)
            if target and not policy.read_allowed(target):
                record("READ", "denied", result=f"{target} not in read allowlist")
                return PermissionResultDeny(
                    message=f"{target!r} is not in the company read allowlist",
                    interrupt=False,
                )
            record("READ", "ok", approver="auto-read")
            return PermissionResultAllow()

        # 3) LOCAL_WRITE inside an approved series → allow silently.
        if consequence is Consequence.LOCAL_WRITE and automode.active:
            record("LOCAL_WRITE", "ok", approver="auto-mode")
            return PermissionResultAllow()

        # 4) Ask a human. OUTWARD always reaches here (even in auto-mode).
        severe = consequence is Consequence.OUTWARD
        decision = await approval.request(
            ApprovalRequest(
                tool=tool_name, args=tool_input, risk=consequence.name,
                session_id=session_id, summary=why, severe=severe,
            )
        )
        if not decision.approved:
            record(consequence.name, "rejected", result=decision.reason)
            return PermissionResultDeny(
                message=decision.reason or "rejected by human", interrupt=True
            )
        if consequence is Consequence.LOCAL_WRITE and decision.scope == "auto":
            automode.enable()
        record(consequence.name, "ok", approver=decision.approver)
        return PermissionResultAllow()

    return can_use_tool
