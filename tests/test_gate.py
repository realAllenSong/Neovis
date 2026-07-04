"""Consequence-gated permission logic — the heart of the Agent SDK architecture.

No model required: we call the `can_use_tool` callback directly with tool
names + inputs and assert the allow/deny decision, the auto-mode transitions,
and that outward actions can never be auto-approved away.
"""

import pytest

from neovis.core.approval import AutoApprove, DenyAll
from neovis.core.audit import AuditLog
from neovis.core.gate import AutoMode, Consequence, build_can_use_tool, classify
from neovis.core.policy import PolicyConfig


# ── classification ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tool,inp,expected", [
    ("Read", {"file_path": "/x"}, Consequence.READ),
    ("Grep", {"pattern": "foo"}, Consequence.READ),
    ("Write", {"file_path": "/x", "content": "y"}, Consequence.LOCAL_WRITE),
    ("Edit", {"file_path": "/x"}, Consequence.LOCAL_WRITE),
    ("Bash", {"command": "ls -la ~/proj"}, Consequence.READ),
    ("Bash", {"command": "git status"}, Consequence.READ),
    ("Bash", {"command": "python backtest.py"}, Consequence.LOCAL_WRITE),
    ("Bash", {"command": "git push origin main"}, Consequence.OUTWARD),
    ("Bash", {"command": "rm -rf build/"}, Consequence.OUTWARD),
    ("mcp__chrome-devtools__navigate_page", {"url": "https://mail.example.com"}, Consequence.READ),
    ("mcp__chrome-devtools__take_snapshot", {}, Consequence.READ),
    ("mcp__chrome-devtools__click", {"element": "Compose button"}, Consequence.LOCAL_WRITE),
    ("mcp__chrome-devtools__click", {"element": "Send email button"}, Consequence.OUTWARD),
])
def test_classify(tool, inp, expected):
    assert classify(tool, inp, PolicyConfig())[0] is expected


# ── helpers ───────────────────────────────────────────────────────────────────
def _gate(tmp_path, approval, automode, policy=None):
    audit = AuditLog(tmp_path / "audit.db")
    cb = build_can_use_tool(policy or PolicyConfig(), audit, approval, automode)
    return cb, audit


def _allowed(result) -> bool:
    return result.behavior == "allow"


# ── behaviour ─────────────────────────────────────────────────────────────────
async def test_denylist_is_hard_deny_even_when_auto_approving(tmp_path):
    cb, audit = _gate(tmp_path, AutoApprove(), AutoMode())
    res = await cb("Bash", {"command": "rm -rf / --no-preserve-root"}, None)
    assert not _allowed(res)
    assert audit.recent()[0]["status"] == "denied"


async def test_read_allowed_without_asking(tmp_path):
    # DenyAll would reject anything that reaches approval; a read must not.
    cb, _ = _gate(tmp_path, DenyAll(), AutoMode())
    res = await cb("Read", {"file_path": "/Users/me/notes.txt"}, None)
    assert _allowed(res)


async def test_read_blocked_by_allowlist(tmp_path):
    policy = PolicyConfig(read_allowlist=["/Users/me/work"])
    cb, audit = _gate(tmp_path, AutoApprove(), AutoMode(), policy)
    res = await cb("Read", {"file_path": "/etc/passwd"}, None)
    assert not _allowed(res)
    assert audit.recent()[0]["status"] == "denied"


async def test_local_write_needs_approval_when_not_in_auto_mode(tmp_path):
    cb, audit = _gate(tmp_path, DenyAll(), AutoMode())
    res = await cb("Write", {"file_path": "/Users/me/x", "content": "y"}, None)
    assert not _allowed(res)
    assert audit.recent()[0]["status"] == "rejected"


async def test_approve_auto_enters_auto_mode(tmp_path):
    automode = AutoMode()
    cb, _ = _gate(tmp_path, AutoApprove(scope="auto"), automode)
    res = await cb("Write", {"file_path": "/Users/me/x", "content": "y"}, None)
    assert _allowed(res)
    assert automode.active  # the series is now auto-approved


async def test_local_write_silent_in_auto_mode(tmp_path):
    # DenyAll proves the approval gateway is NOT consulted once auto-mode is on.
    automode = AutoMode(active=True)
    cb, audit = _gate(tmp_path, DenyAll(), automode)
    res = await cb("Edit", {"file_path": "/Users/me/x"}, None)
    assert _allowed(res)
    assert audit.recent()[0]["approver"] == "auto-mode"


async def test_outward_always_asks_even_in_auto_mode(tmp_path):
    # Auto-mode is on, but an outward action must still reach approval → DenyAll blocks it.
    automode = AutoMode(active=True)
    cb, audit = _gate(tmp_path, DenyAll(), automode)
    res = await cb("mcp__chrome-devtools__click", {"element": "Send email"}, None)
    assert not _allowed(res)
    assert audit.recent()[0]["status"] == "rejected"


async def test_outward_approved_executes(tmp_path):
    cb, audit = _gate(tmp_path, AutoApprove(), AutoMode())
    res = await cb("Bash", {"command": "git push origin main"}, None)
    assert _allowed(res)
    row = audit.recent()[0]
    assert row["tool"] == "Bash" and row["status"] == "ok"
