"""Neovis command line: an interactive REPL, plus a no-API-key self-test.

    neovis                 # talk to the agent (needs NEOVIS_API_KEY)
    neovis --self-test     # drive the whole pipeline with a fake model, no key
    neovis --auto-approve  # skip approval prompts (demo only)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .core.agent import Session, build_agent
from .core.approval import AutoApprove, ConsoleApproval
from .core.audit import AuditLog
from .core.config import load_config
from .core.deps import NeovisDeps
from . import tools  # noqa: F401  (registers built-in tools)


def _print_audit(audit: AuditLog, limit: int = 10) -> None:
    rows = audit.recent(limit)
    if not rows:
        print("(audit log empty)")
        return
    print("\n── recent audit ──")
    for r in reversed(rows):
        line = f"  {r['tool']:<16} [{r['risk']:<9}] {r['status']}"
        if r["approver"]:
            line += f" (approved by {r['approver']})"
        print(line)
    print("──────────────────\n")


def _build_session(args, model=None) -> tuple[Session, AuditLog]:
    config = load_config() if model is None else _selftest_config()
    if args.auto_approve:
        config.policy.auto_approve_dangerous = True
    audit = AuditLog(args.audit_db)
    gateway = AutoApprove() if (args.auto_approve or model is not None) else ConsoleApproval()
    agent = build_agent(config, model=model)
    deps = NeovisDeps(
        policy=config.policy,
        audit=audit,
        approval=gateway,
        session_id="cli",
        actor="cli",
    )
    return Session(agent=agent, deps=deps), audit


def _selftest_config():
    """Minimal config so --self-test never touches models.yaml or a real key."""
    from .core.config import AppConfig, ModelConfig

    return AppConfig(llm=ModelConfig(provider="openai", model="test"))


def _selftest_model():
    """A scripted FunctionModel that exercises safe + dangerous tools, no network."""
    from pydantic_ai import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.messages import ModelMessage

    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Count how many model responses we've already produced (== tool rounds).
        rounds = sum(1 for m in messages if m.kind == "response")
        if rounds == 0:
            return ModelResponse(parts=[ToolCallPart("system_status", {})])
        if rounds == 1:
            return ModelResponse(parts=[ToolCallPart("list_files", {"directory": "."})])
        if rounds == 2:
            return ModelResponse(
                parts=[ToolCallPart("run_shell", {"command": "echo neovis-selftest"})]
            )
        return ModelResponse(
            parts=[TextPart("Self-test complete: safe + dangerous tool paths exercised.")]
        )

    return FunctionModel(script)


async def _run_selftest(args) -> int:
    print("Neovis self-test (no API key, scripted model)\n")
    session, audit = _build_session(args, model=_selftest_model())
    output = await session.send("Run a self-test.")
    print("Agent:", output)
    _print_audit(audit)
    statuses = {r["status"] for r in audit.recent(20)}
    ok = "ok" in statuses
    print("RESULT:", "PASS ✅" if ok else "FAIL ❌")
    audit.close()
    return 0 if ok else 1


async def _run_repl(args) -> int:
    if not os.environ.get("NEOVIS_API_KEY"):
        print("NEOVIS_API_KEY is not set. Use --self-test to try without a key.")
        return 2
    try:
        session, audit = _build_session(args)
    except Exception as exc:
        print(f"Startup failed: {exc}")
        return 2

    print("Neovis ready. Type a request, or /audit, /stop, /quit.\n")
    loop = asyncio.get_event_loop()
    while True:
        try:
            line = (await asyncio.to_thread(input, "you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/audit":
            _print_audit(audit)
            continue
        if line == "/stop":
            session.stop()
            print("(stop signalled)")
            continue
        try:
            output = await session.send(line)
            print("neovis>", output)
        except Exception as exc:
            print(f"(error) {exc}")
    audit.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="neovis", description="Workstation agent.")
    parser.add_argument("--self-test", action="store_true", help="run scripted pipeline test, no API key")
    parser.add_argument("--auto-approve", action="store_true", help="auto-approve dangerous actions (demo)")
    parser.add_argument("--audit-db", default="neovis_audit.db", help="path to the SQLite audit log")
    args = parser.parse_args()

    runner = _run_selftest if args.self_test else _run_repl
    sys.exit(asyncio.run(runner(args)))


if __name__ == "__main__":
    main()
