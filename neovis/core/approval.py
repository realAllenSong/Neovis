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


@dataclass
class ApprovalDecision:
    approved: bool
    approver: str | None = None
    reason: str | None = None


class ApprovalGateway(ABC):
    """Approve or reject a dangerous action. Implementations may block (await)."""

    @abstractmethod
    async def request(self, req: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprove(ApprovalGateway):
    """Non-interactive: approve everything. For tests and (opt-in) demos only."""

    def __init__(self, approver: str = "auto"):
        self._approver = approver

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=True, approver=self._approver)


class DenyAll(ApprovalGateway):
    """Reject every dangerous action. Useful as a safe default in unit tests."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(approved=False, reason="denied by policy (DenyAll)")


class ConsoleApproval(ApprovalGateway):
    """Prompt on stdin. Used by the CLI REPL."""

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        import asyncio

        prompt = (
            f"\n⚠️  Approve DANGEROUS action?\n"
            f"    tool: {req.tool}\n"
            f"    args: {req.args}\n"
            f"    [y/N] > "
        )
        answer = await asyncio.to_thread(input, prompt)
        ok = answer.strip().lower() in ("y", "yes")
        return ApprovalDecision(
            approved=ok,
            approver="console" if ok else None,
            reason=None if ok else "rejected at console",
        )
