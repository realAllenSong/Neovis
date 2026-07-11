"""Pinned auto-mode: survives turns, one approval to enable, free to disable,
and OUTWARD still always confirms."""

from __future__ import annotations

import pytest

from neovis.core.approval import ApprovalDecision, ApprovalGateway
from neovis.core.audit import AuditLog
from neovis.core.config import PolicyConfig
from neovis.core.gate import AutoMode, Consequence, build_evaluator, classify


def test_series_automode_clears_on_reset():
    m = AutoMode()
    m.enable()
    m.reset()
    assert not m.active


def test_pinned_automode_survives_reset():
    m = AutoMode()
    m.pin()
    m.reset()          # end of turn
    assert m.active    # still on
    m.unpin()
    m.reset()
    assert not m.active


def test_gate_tiers_for_control_tool():
    tier_on, why = classify("mcp__neovis-control__auto_mode", {"enable": True}, PolicyConfig())
    tier_off, _ = classify("mcp__neovis-control__auto_mode", {"enable": False}, PolicyConfig())
    assert tier_on is Consequence.LOCAL_WRITE and "pins" in why  # confirm once
    assert tier_off is Consequence.READ                          # tightening is free


class _CountingApproval(ApprovalGateway):
    def __init__(self):
        self.calls = 0

    async def request(self, req):
        self.calls += 1
        return ApprovalDecision(True, approver="test", scope="once")


@pytest.mark.anyio
async def test_pinned_mode_skips_write_prompts_but_not_outward(tmp_path):
    approval = _CountingApproval()
    mode = AutoMode()
    mode.pin()
    evaluate = build_evaluator(
        PolicyConfig(), AuditLog(str(tmp_path / "a.db")), approval, mode)

    d = await evaluate("Write", {"file_path": "/tmp/x", "content": "hi"})
    assert d.allow and approval.calls == 0          # pinned: no prompt

    mode.reset()                                     # a new turn begins
    d = await evaluate("Write", {"file_path": "/tmp/y", "content": "hi"})
    assert d.allow and approval.calls == 0          # still pinned

    d = await evaluate("Bash", {"command": "git push origin main"})
    assert approval.calls == 1                      # OUTWARD always confirms


@pytest.fixture
def anyio_backend():
    return "asyncio"
