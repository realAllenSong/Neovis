"""The permission gate — Neovis's consequence-gated tool control.

We classify every tool call by *consequence*, not by tool, and apply the user's
policy:

* READ          → allow after an allowlist check (no interruption)
* LOCAL_WRITE   → approve once; "approve + auto" enters auto-mode for the series
* OUTWARD       → always ask a human, even in auto-mode; severe ones double-confirm

A denylisted shell command is a hard deny that approval cannot override.

The decision is exposed to the Claude Agent SDK as a **PreToolUse hook**
(:func:`build_pre_tool_use_hook`), which fires for *every* tool call regardless
of permission mode — unlike ``can_use_tool``, which only fires when the engine
decides a prompt is needed. :func:`build_can_use_tool` wraps the same logic for
the callback API (and for unit tests). "Asking a human" is done by awaiting an
:class:`ApprovalGateway` (Slack buttons on the phone, or the console).
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


_READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead", "WebSearch", "TodoWrite"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

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


@dataclass
class Decision:
    allow: bool
    reason: str
    tier: str


def _read_target(tool_input: dict[str, Any]) -> str:
    for key in ("file_path", "path", "url", "pattern", "query"):
        if tool_input.get(key):
            return str(tool_input[key])
    return ""


def build_evaluator(
    policy: PolicyConfig,
    audit: AuditLog,
    approval: ApprovalGateway,
    automode: AutoMode,
    *,
    actor: str = "user",
    session_id: str = "desktop",
):
    """The shared decision core used by both the hook and the callback."""

    async def evaluate(tool_name: str, tool_input: dict[str, Any]) -> Decision:
        tool_input = tool_input or {}

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
                return Decision(False, f"blocked by denylist rule /{rule}/", "DENYLIST")

        consequence, why = classify(tool_name, tool_input, policy)

        # 2) READ — allow after the allowlist check; never interrupt the user.
        if consequence is Consequence.READ:
            target = _read_target(tool_input)
            if target and not policy.read_allowed(target):
                record("READ", "denied", result=f"{target} not in read allowlist")
                return Decision(False, f"{target!r} is not in the company read allowlist", "READ")
            record("READ", "ok", approver="auto-read")
            return Decision(True, why, "READ")

        # 3) LOCAL_WRITE inside an approved series → allow silently.
        if consequence is Consequence.LOCAL_WRITE and automode.active:
            record("LOCAL_WRITE", "ok", approver="auto-mode")
            return Decision(True, "auto-mode", "LOCAL_WRITE")

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
            return Decision(False, decision.reason or "rejected by human", consequence.name)
        if consequence is Consequence.LOCAL_WRITE and decision.scope == "auto":
            automode.enable()
        record(consequence.name, "ok", approver=decision.approver)
        return Decision(True, why, consequence.name)

    return evaluate


def build_pre_tool_use_hook(
    policy: PolicyConfig,
    audit: AuditLog,
    approval: ApprovalGateway,
    automode: AutoMode,
    *,
    actor: str = "user",
    session_id: str = "desktop",
):
    """A PreToolUse hook for ClaudeAgentOptions.hooks — fires on every tool call."""
    evaluate = build_evaluator(
        policy, audit, approval, automode, actor=actor, session_id=session_id
    )

    async def hook(input_data: Any, tool_use_id: Any, context: Any) -> dict:
        get = input_data.get if isinstance(input_data, dict) else (lambda k, d=None: getattr(input_data, k, d))
        tool_name = get("tool_name") or ""
        tool_input = get("tool_input") or {}
        decision = await evaluate(tool_name, tool_input)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if decision.allow else "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    return hook


def build_can_use_tool(
    policy: PolicyConfig,
    audit: AuditLog,
    approval: ApprovalGateway,
    automode: AutoMode,
    *,
    actor: str = "user",
    session_id: str = "desktop",
):
    """A can_use_tool callback (same logic) — used in unit tests and as a fallback."""
    evaluate = build_evaluator(
        policy, audit, approval, automode, actor=actor, session_id=session_id
    )

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any):
        decision = await evaluate(tool_name, tool_input)
        if decision.allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.reason, interrupt=True)

    return can_use_tool
