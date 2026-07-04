"""Shell execution — the workhorse, and the highest-risk tool.

DANGEROUS: every call is gated by approval, and the denylist precheck hard-
blocks catastrophic commands even if a human would have approved them.
"""

from __future__ import annotations

import subprocess

from ..core.registry import tool
from ._guards import shell_precheck


@tool(
    risk="dangerous",
    precheck=shell_precheck,
    description=(
        "Run a shell command on this computer and return its exit code, stdout "
        "and stderr. Use for file ops, running scripts, git, package managers, "
        "etc. Prefer a single self-contained command. Destructive commands are "
        "blocked by policy."
    ),
)
def run_shell(command: str, workdir: str = "", timeout_seconds: int = 60) -> str:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=workdir or None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: command exceeded {timeout_seconds}s and was killed."

    out = (proc.stdout or "")[-6000:]
    err = (proc.stderr or "")[-2000:]
    parts = [f"exit_code={proc.returncode}"]
    if out.strip():
        parts.append(f"stdout:\n{out}")
    if err.strip():
        parts.append(f"stderr:\n{err}")
    if not out.strip() and not err.strip():
        parts.append("(no output)")
    return "\n".join(parts)
