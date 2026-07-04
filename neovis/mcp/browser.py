"""Browser control via the chrome-devtools MCP server.

Two modes:

* **Demo / real work** — connect to the user's *already-logged-in* Chrome so the
  agent acts with their real session (the "email the CTO" flow). Start Chrome
  with ``--remote-debugging-port=9222`` and pass ``browser_url``.
* **Safe smoke test** — ``isolated=True, headless=True`` launches a throwaway
  Chrome with a temporary profile, touching none of the user's logins.

Browser tool calls (``mcp__chrome-devtools__*``) flow through the same
consequence gate: navigation/snapshots are READ (auto-allowed), clicks are
LOCAL_WRITE, and a click whose target reads like Send/Submit is OUTWARD
(always confirmed).
"""

from __future__ import annotations

_PKG = "chrome-devtools-mcp@latest"


def chrome_devtools_mcp(
    *,
    browser_url: str | None = None,
    headless: bool = False,
    isolated: bool = False,
    slim: bool = False,
    blocked_url_patterns: list[str] | None = None,
    extra_args: list[str] | None = None,
    name: str = "chrome-devtools",
) -> dict:
    """Return an mcp_servers entry for ClaudeAgentOptions.

    Pass ``browser_url`` (e.g. ``http://127.0.0.1:9222``) to drive a running,
    logged-in Chrome; otherwise the server launches its own instance.
    """
    args = ["-y", _PKG]
    if browser_url:
        args += ["--browserUrl", browser_url]
    if headless:
        args.append("--headless")
    if isolated:
        args.append("--isolated")
    if slim:
        args.append("--slim")
    for pattern in blocked_url_patterns or []:
        args += ["--blockedUrlPattern", pattern]
    if extra_args:
        args += extra_args

    return {
        name: {
            "type": "stdio",
            "command": "npx",
            "args": args,
            # Don't phone home usage stats from a fund's machine.
            "env": {"CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1"},
        }
    }
