"""System inspection tools (SAFE, read-only): processes and resource usage."""

from __future__ import annotations

from ..core.registry import tool


@tool(
    risk="safe",
    description="Report CPU, memory and disk usage of this computer.",
)
def system_status() -> str:
    import psutil

    cpu = psutil.cpu_percent(interval=0.3)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    return (
        f"CPU: {cpu:.0f}%\n"
        f"Memory: {vm.percent:.0f}% used "
        f"({vm.used // (1024**2)}MB / {vm.total // (1024**2)}MB)\n"
        f"Disk (/): {du.percent:.0f}% used "
        f"({du.used // (1024**3)}GB / {du.total // (1024**3)}GB)"
    )


@tool(
    risk="safe",
    description="List running processes, optionally filtered by a name substring "
    "(case-insensitive), sorted by memory use.",
)
def list_processes(name_filter: str = "", limit: int = 20) -> str:
    import psutil

    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
        info = p.info
        nm = info.get("name") or ""
        if name_filter and name_filter.lower() not in nm.lower():
            continue
        procs.append((info.get("memory_percent") or 0.0, info["pid"], nm))
    procs.sort(reverse=True)
    if not procs:
        return f"No processes match {name_filter!r}." if name_filter else "No processes found."
    lines = [f"Top {min(limit, len(procs))} process(es) by memory:"]
    for mem, pid, nm in procs[:limit]:
        lines.append(f"  pid={pid:<7} mem={mem:4.1f}%  {nm}")
    return "\n".join(lines)
