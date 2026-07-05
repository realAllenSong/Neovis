"""Neovis interactive REPL — talk to the workstation agent from your terminal.

    uv run neovis                      # subscription/proxy Claude; console approval
    uv run neovis --auto-approve       # skip prompts (demo only)
    uv run neovis --browser            # attach chrome-devtools MCP
    uv run neovis --browser-url URL    # drive a running, logged-in Chrome

Writes and outward actions pause for a console approval ([y]es / [a]uto-run rest
/ [N]o); reads run freely. This is the same gate the Slack channel uses on your
phone — here the approval UI is the terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .core.approval import AutoApprove, ConsoleApproval
from .core.audit import AuditLog
from .core.config import AppConfig, ModelConfig, load_config
from .core.session import NeovisSession
from .mcp.browser import chrome_devtools_mcp


def _load_config() -> AppConfig:
    try:
        cfg = load_config()
        cfg.llm.model = cfg.llm.model or ""  # empty => engine/subscription default
        return cfg
    except Exception:
        return AppConfig(llm=ModelConfig(provider="anthropic", model=""))


def _print_audit(audit: AuditLog) -> None:
    rows = audit.recent(15)
    if not rows:
        print("(audit empty)")
        return
    print("\n── recent audit ──")
    for r in reversed(rows):
        line = f"  {r['tool'][:28]:<28} [{r['risk']:<11}] {r['status']}"
        if r["approver"]:
            line += f"  ({r['approver']})"
        print(line)
    print("──────────────────\n")


def _browser_url_reachable(url: str) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=2)
        return True
    except Exception:
        return False


def _trace_tool(name: str, tool_input: dict) -> None:
    """Live one-line trace of a tool the agent is invoking."""
    short = name.rsplit("__", 1)[-1]
    detail = ""
    for key in ("url", "command", "file_path", "path", "value", "uid", "query"):
        if tool_input.get(key):
            detail = f"{key}={str(tool_input[key])[:60]}"
            break
    print(f"   · {short} {detail}".rstrip())


async def _run(args) -> int:
    config = _load_config()
    audit = AuditLog(args.audit_db)
    approval = AutoApprove() if args.auto_approve else ConsoleApproval()

    browser = bool(args.browser or args.browser_url)
    mcp_servers = None
    disallowed = None
    if browser:
        # Preflight: a common trap is running the launch command while Chrome is
        # already open, which silently ignores --remote-debugging-port.
        if args.browser_url and not _browser_url_reachable(args.browser_url):
            print(f"Chrome debug port not reachable at {args.browser_url}.")
            print("Fix — QUIT Chrome completely first (Cmd-Q), THEN run:")
            print('  open -a "Google Chrome" --args --remote-debugging-port=9222')
            print("(the flag is ignored if Chrome is already running).")
            return 2
        mcp_servers = chrome_devtools_mcp(browser_url=args.browser_url)
        # In browser mode the agent must use the browser tools, not the shell.
        disallowed = ["Bash"]

    session = NeovisSession(
        config, approval=approval, audit=audit,
        gateway_url=args.gateway_url, gateway_key=args.gateway_key,
        mcp_servers=mcp_servers, disallowed_tools=disallowed, browser=browser,
    )
    try:
        await session.connect()
    except Exception as exc:
        print(f"Startup failed: {exc}")
        return 2

    print("Neovis ready. Type a request, or /audit, /quit.")
    if browser:
        print("Browser attached — watch your Chrome window; steps trace below.")
    print()
    try:
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
            try:
                out = await session.send(line, on_tool=_trace_tool)
                print("neovis>", out)
            except Exception as exc:
                print(f"(error) {exc}")
    finally:
        await session.disconnect()
        audit.close()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="neovis", description="Workstation agent REPL.")
    p.add_argument("--auto-approve", action="store_true", help="skip approval prompts (demo only)")
    p.add_argument("--browser", action="store_true", help="attach chrome-devtools MCP (own Chrome)")
    p.add_argument("--browser-url", default=None, help="drive a running Chrome (e.g. http://127.0.0.1:9222)")
    p.add_argument("--gateway-url", default=None, help="model endpoint base_url (proxy/gateway)")
    p.add_argument("--gateway-key", default=None, help="key for the gateway")
    p.add_argument("--audit-db", default="neovis_audit.db", help="SQLite audit log path")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
