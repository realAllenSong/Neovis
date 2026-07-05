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

import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

_PKG = "chrome-devtools-mcp@latest"

# Persistent, dedicated Chrome profile for Neovis. The user logs into Gmail (and
# any other site Neovis needs) here ONCE; it survives across sessions. Chrome
# 136+ refuses --remote-debugging-port on the *default* logged-in profile for
# security, so a dedicated profile is both required and safer (Neovis only sees
# what you log into here, not your whole main browser).
NEOVIS_CHROME_PROFILE = Path.home() / ".neovis" / "chrome-profile"
_MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _chrome_binary() -> str | None:
    for candidate in (
        _MAC_CHROME,
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chrome"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def debug_port_up(port: int = 9222, host: str = "127.0.0.1") -> bool:
    try:
        urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def launch_neovis_chrome(
    port: int = 9222,
    user_data_dir: Path | str = NEOVIS_CHROME_PROFILE,
    *,
    wait: float = 20.0,
) -> bool:
    """Launch the dedicated Neovis Chrome with remote debugging (cross-platform).

    Uses the binary directly (macOS ``open --args`` is unreliable). Returns True
    once the debug port answers. If it's already up, returns immediately.
    """
    if debug_port_up(port):
        return True
    chrome = _chrome_binary()
    if not chrome:
        return False
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if debug_port_up(port):
            return True
        time.sleep(0.5)
    return False


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
