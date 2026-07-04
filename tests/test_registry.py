"""The security-critical behaviour: enforcement in the tool registry.

These run the *real* agent loop against a scripted FunctionModel, so they prove
the gate holds end-to-end — not just that a helper returns the right bool.
"""

from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from neovis.core.agent import Session, build_agent
from neovis.core.approval import AutoApprove, DenyAll
from neovis.core.audit import AuditLog
from neovis.core.config import AppConfig, ModelConfig
from neovis.core.deps import NeovisDeps
from neovis.core.policy import PolicyConfig
from neovis.core.registry import ToolRegistry, tool
from neovis.tools._guards import sandbox_precheck, shell_precheck


def _one_call_model(tool_name: str, args: dict) -> FunctionModel:
    """Model that calls one tool then finishes."""

    def script(messages, info):
        rounds = sum(1 for m in messages if m.kind == "response")
        if rounds == 0:
            return ModelResponse(parts=[ToolCallPart(tool_name, args)])
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(script)


def _session(registry, gateway, policy, db_path, model) -> tuple[Session, AuditLog]:
    config = AppConfig(llm=ModelConfig(provider="openai", model="x"), policy=policy)
    agent = build_agent(config, registry=registry, model=model)
    audit = AuditLog(db_path)
    deps = NeovisDeps(policy=policy, audit=audit, approval=gateway)
    return Session(agent=agent, deps=deps), audit


async def test_dangerous_rejected_does_not_execute(tmp_path):
    reg = ToolRegistry()
    ran: list[str] = []

    @tool(risk="dangerous", registry=reg, description="danger")
    def do_danger(x: str) -> str:
        ran.append(x)
        return "ran"

    session, audit = _session(
        reg, DenyAll(), PolicyConfig(), tmp_path / "a.db",
        _one_call_model("do_danger", {"x": "hi"}),
    )
    await session.send("go")

    assert ran == []  # the human said no → the side effect never happened
    assert "rejected" in {r["status"] for r in audit.recent()}


async def test_dangerous_approved_executes(tmp_path):
    reg = ToolRegistry()
    ran: list[str] = []

    @tool(risk="dangerous", registry=reg, description="danger")
    def do_danger(x: str) -> str:
        ran.append(x)
        return "ran"

    session, audit = _session(
        reg, AutoApprove(), PolicyConfig(), tmp_path / "a.db",
        _one_call_model("do_danger", {"x": "hi"}),
    )
    await session.send("go")

    assert ran == ["hi"]
    row = next(r for r in audit.recent() if r["tool"] == "do_danger")
    assert row["status"] == "ok"
    assert row["approver"] == "auto"


async def test_denylist_beats_auto_approve(tmp_path):
    """A denylisted command is refused even when everything is auto-approved."""
    reg = ToolRegistry()
    ran: list[str] = []

    @tool(risk="dangerous", registry=reg, precheck=shell_precheck, description="sh")
    def sh(command: str) -> str:
        ran.append(command)
        return "ran"

    policy = PolicyConfig(auto_approve_dangerous=True)
    session, audit = _session(
        reg, AutoApprove(), policy, tmp_path / "a.db",
        _one_call_model("sh", {"command": "rm -rf / --no-preserve-root"}),
    )
    await session.send("go")

    assert ran == []  # never executed despite auto-approve
    assert audit.recent()[0]["status"] == "denied"


async def test_sandbox_blocks_write_outside_root(tmp_path):
    reg = ToolRegistry()

    @tool(risk="moderate", registry=reg, precheck=sandbox_precheck("path"), description="w")
    def w(path: str, content: str) -> str:
        Path(path).write_text(content)
        return "wrote"

    target = tmp_path / "outside" / "evil.txt"  # not under the sandbox root below
    policy = PolicyConfig(sandbox_roots=[str(tmp_path / "allowed")])
    session, audit = _session(
        reg, AutoApprove(), policy, tmp_path / "a.db",
        _one_call_model("w", {"path": str(target), "content": "x"}),
    )
    await session.send("go")

    assert not target.exists()
    assert audit.recent()[0]["status"] == "denied"


async def test_safe_tool_runs_and_is_audited(tmp_path):
    reg = ToolRegistry()

    @tool(risk="safe", registry=reg, description="ping")
    def ping() -> str:
        return "pong"

    session, audit = _session(
        reg, DenyAll(), PolicyConfig(), tmp_path / "a.db",
        _one_call_model("ping", {}),
    )
    await session.send("go")

    row = audit.recent()[0]
    assert row["tool"] == "ping" and row["status"] == "ok"
