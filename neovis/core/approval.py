"""Approval gateways.

When a DANGEROUS tool is about to run, the registry asks a gateway to approve
it. The gateway abstraction lets the same agent core work whether the human is
at a console, on Slack (interactive buttons), or auto-approving in a demo.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ApprovalRequest:
    tool: str
    args: dict
    risk: str
    session_id: str | None = None
    summary: str | None = None  # short human-readable "about to run X"
    severe: bool = False        # outward/irreversible → request double-confirmation


@dataclass
class ApprovalDecision:
    approved: bool
    approver: str | None = None
    reason: str | None = None
    # "once" = approve just this action; "auto" = approve and auto-run the rest
    # of this series (enters auto-mode until the task ends). Only honoured for
    # LOCAL_WRITE actions — outward actions always re-ask.
    scope: str = "once"


class ApprovalGateway(ABC):
    """Approve or reject a dangerous action. Implementations may block (await)."""

    @abstractmethod
    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprove(ApprovalGateway):
    """Non-interactive: approve everything. For tests and (opt-in) demos only."""

    def __init__(self, approver: str = "auto", scope: str = "once"):
        self._approver = approver
        self._scope = scope

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True, approver=self._approver, scope=self._scope)


class DenyAll(ApprovalGateway):
    """Reject every dangerous action. Useful as a safe default in unit tests."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=False, reason="denied by policy (DenyAll)")


class ConsoleApproval(ApprovalGateway):
    """Prompt on stdin. Used by the CLI REPL."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        import asyncio

        tier = "OUTWARD/irreversible" if req.severe else req.risk
        prompt = (
            f"\n⚠️  Approve {tier} action?\n"
            f"    tool: {req.tool}\n"
            f"    args: {req.args}\n"
            f"    [y]es / [a]uto-run rest / [N]o > "
        )
        answer = (await asyncio.to_thread(input, prompt)).strip().lower()
        if answer in ("a", "auto"):
            approved, scope = True, "auto"
        elif answer in ("y", "yes"):
            approved, scope = True, "once"
        else:
            approved, scope = False, "once"

        # Outward/irreversible actions require a second confirmation.
        if approved and req.severe:
            confirm = (await asyncio.to_thread(
                input, "    This is irreversible. Type 'send' to confirm > "
            )).strip().lower()
            if confirm != "send":
                return ApprovalDecision(approved=False, reason="double-confirm failed")

        return ApprovalDecision(
            approved=approved,
            approver="console" if approved else None,
            reason=None if approved else "rejected at console",
            scope=scope,
        )
