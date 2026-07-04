"""Filesystem tools: inspect (SAFE) and modify (MODERATE, sandbox-checked)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..core.registry import tool
from ._guards import sandbox_precheck


@tool(
    risk="safe",
    description="List files and folders in a directory, optionally filtered by a "
    "glob pattern (e.g. '*.xlsx'). Returns names, sizes and types.",
)
def list_files(directory: str = ".", pattern: str = "*") -> str:
    base = Path(directory).expanduser()
    if not base.exists():
        return f"No such directory: {directory}"
    entries = sorted(base.glob(pattern))
    if not entries:
        return f"No entries in {base} matching {pattern!r}."
    lines = [f"{base} ({len(entries)} entries):"]
    for p in entries[:200]:
        kind = "dir " if p.is_dir() else "file"
        size = p.stat().st_size if p.is_file() else 0
        lines.append(f"  [{kind}] {p.name}" + (f"  ({size}B)" if p.is_file() else ""))
    return "\n".join(lines)


@tool(
    risk="safe",
    description="Search a directory tree for files whose name contains a query "
    "substring (case-insensitive). Good for 'find the Q2 files'.",
)
def search_files(directory: str, query: str, max_results: int = 50) -> str:
    base = Path(directory).expanduser()
    if not base.exists():
        return f"No such directory: {directory}"
    q = query.lower()
    hits = [
        p for p in base.rglob("*")
        if p.is_file() and q in p.name.lower()
    ][:max_results]
    if not hits:
        return f"No files under {base} match {query!r}."
    return f"{len(hits)} match(es):\n" + "\n".join(f"  {p}" for p in hits)


@tool(
    risk="safe",
    description="Read a UTF-8 text file and return its contents (truncated if large).",
)
def read_file(path: str, max_bytes: int = 40000) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"No such file: {path}"
    data = p.read_bytes()[:max_bytes]
    text = data.decode("utf-8", errors="replace")
    suffix = "" if p.stat().st_size <= max_bytes else f"\n… [truncated at {max_bytes} bytes]"
    return text + suffix


@tool(
    risk="moderate",
    precheck=sandbox_precheck("path"),
    description="Write UTF-8 text to a file, creating parent folders. Overwrites. "
    "Restricted to the configured sandbox roots.",
)
def write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {p}."


@tool(
    risk="moderate",
    precheck=sandbox_precheck("output_path"),
    description="Zip a directory into an archive so it can be sent back to the "
    "user. Returns the archive path. Restricted to the sandbox roots.",
)
def make_zip(directory: str, output_path: str) -> str:
    src = Path(directory).expanduser()
    if not src.exists():
        return f"No such directory: {directory}"
    out = Path(output_path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src))
                count += 1
    return f"Zipped {count} file(s) into {out} ({out.stat().st_size} bytes)."
