"""Code intelligence via codebase-memory-mcp (DeusData).

A local knowledge graph over source code: tree-sitter parses repos into a
SQLite graph (who calls what, imports, routes), and the agent queries structure
instead of grepping file by file — a "which modules call OrderRouter?" from
your phone answers in one cheap query instead of a token-burning crawl.

Single static binary, local-only (index cache in ~/.cache/codebase-memory-mcp).
Install:  curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash -s -- --skip-config

Gate tiers: graph queries and indexing are READ (reads repos, writes only its
own cache); mutations (delete_project, manage_adr, ingest_traces) stay at the
default LOCAL_WRITE and prompt.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Read-tier tools: pure queries, plus indexing (reads the repo, writes only
# its own cache — nothing in the working tree is touched).
CODE_INTEL_READ_TOOLS = frozenset({
    "index_repository", "index_status", "list_projects",
    "search_graph", "trace_path", "detect_changes", "query_graph",
    "get_graph_schema", "get_code_snippet", "get_architecture", "search_code",
})


def code_intel_binary() -> str | None:
    """The codebase-memory-mcp binary, or None if not installed."""
    hit = shutil.which("codebase-memory-mcp")
    if hit:
        return hit
    fallback = Path.home() / ".local" / "bin" / "codebase-memory-mcp"
    return str(fallback) if fallback.exists() else None


def code_intel_mcp(name: str = "codebase-memory") -> dict:
    """An mcp_servers entry for ClaudeAgentOptions; {} if the binary is absent
    (sessions then simply run without code intelligence)."""
    binary = code_intel_binary()
    if not binary:
        return {}
    return {
        name: {
            "type": "stdio",
            "command": binary,
            "args": [],
            "env": dict(os.environ),
        }
    }
