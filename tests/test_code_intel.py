"""codebase-memory-mcp wiring: config shape and gate tiers."""

from __future__ import annotations

from neovis.core.config import PolicyConfig
from neovis.core.gate import Consequence, classify
from neovis.mcp.code import CODE_INTEL_READ_TOOLS, code_intel_binary, code_intel_mcp


def test_config_shape_matches_binary_presence():
    cfg = code_intel_mcp()
    if code_intel_binary():
        entry = cfg["codebase-memory"]
        assert entry["type"] == "stdio" and entry["command"].endswith("codebase-memory-mcp")
    else:
        assert cfg == {}  # sessions run fine without it


def test_gate_queries_are_read():
    for action in ("search_graph", "trace_path", "query_graph", "get_architecture",
                   "search_code", "index_repository"):
        assert action in CODE_INTEL_READ_TOOLS
        tier, why = classify(f"mcp__codebase-memory__{action}", {}, PolicyConfig())
        assert tier is Consequence.READ, action
        assert "code-graph" in why


def test_gate_mutations_still_prompt():
    for action in ("delete_project", "manage_adr", "ingest_traces"):
        tier, _ = classify(f"mcp__codebase-memory__{action}", {}, PolicyConfig())
        assert tier is Consequence.LOCAL_WRITE, action
