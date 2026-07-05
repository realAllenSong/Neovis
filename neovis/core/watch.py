"""Proactive watchers — the "kick off a long job, I'll ping you when it's done" half.

The agent registers a watch and returns immediately (it isn't blocked). A watch
runs as a background asyncio task; when it fires, the channel's notifier pushes to
the user — a Slack DM on the phone, or spoken aloud at the desk. Watch a shell
command to completion, a PID to exit, or a glob path to appear.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from glob import glob

from claude_agent_sdk import create_sdk_mcp_server, tool


@dataclass
class WatchResult:
    kind: str          # command | process | file
    target: str
    note: str
    ok: bool
    detail: str


class WatchManager:
    """Owns background watch tasks and calls an async notifier when each fires."""

    def __init__(self, notify, poll: float = 2.0):
        self._notify = notify          # async callable(WatchResult)
        self._poll = poll
        self._tasks: set[asyncio.Task] = set()

    @property
    def active(self) -> int:
        return len(self._tasks)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def watch_command(self, command: str, note: str = "") -> None:
        async def run() -> None:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            tail = (out or b"").decode("utf-8", "replace")[-500:].strip()
            await self._notify(WatchResult(
                "command", command, note, proc.returncode == 0,
                f"exit code {proc.returncode}" + (f"\n{tail}" if tail else ""),
            ))

        self._spawn(run())

    def watch_process(self, pid: int, note: str = "") -> None:
        async def run() -> None:
            import psutil

            while psutil.pid_exists(pid):
                await asyncio.sleep(self._poll)
            await self._notify(WatchResult("process", str(pid), note, True, f"process {pid} has exited"))

        self._spawn(run())

    def watch_file(self, pattern: str, note: str = "") -> None:
        async def run() -> None:
            while not glob(pattern):
                await asyncio.sleep(self._poll)
            hits = glob(pattern)[:5]
            await self._notify(WatchResult("file", pattern, note, True, f"appeared: {', '.join(hits)}"))

        self._spawn(run())

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()


_WATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["command", "process", "file"]},
        "target": {"type": "string", "description": "shell command / PID / glob path"},
        "note": {"type": "string", "description": "short label to show the user when it fires"},
    },
    "required": ["kind", "target"],
    "additionalProperties": False,
}


def build_watch_mcp(manager: WatchManager, policy=None):
    """An in-process MCP server exposing a `watch` tool bound to this manager."""

    @tool(
        "watch",
        "Register a background watch and return immediately so you can keep working — "
        "Neovis notifies the user when it fires. kind='command' runs and watches a shell "
        "command to completion; kind='process' waits for a PID to exit; kind='file' waits "
        "for a glob path to appear. Use for long jobs, e.g. 'run the backtest and ping me'.",
        _WATCH_SCHEMA,
    )
    async def watch(args):
        kind = (args.get("kind") or "").lower()
        target = str(args.get("target") or "").strip()
        note = str(args.get("note") or "").strip()

        if kind == "command":
            if policy is not None and (rule := policy.is_shell_denied(target)):
                return {"content": [{"type": "text", "text": f"DENIED: command matches denylist /{rule}/"}]}
            manager.watch_command(target, note)
        elif kind == "process":
            try:
                manager.watch_process(int(target), note)
            except ValueError:
                return {"content": [{"type": "text", "text": "for kind='process', target must be a PID"}]}
        elif kind == "file":
            manager.watch_file(target, note)
        else:
            return {"content": [{"type": "text", "text": "kind must be command, process, or file"}]}

        return {"content": [{"type": "text", "text": f"Watching {kind} {target!r}. I'll notify you when it's done."}]}

    return create_sdk_mcp_server(name="neovis-watch", tools=[watch])
