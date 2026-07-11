"""Persistent curated memory — Neovis remembers across sessions.

Design borrowed from Hermes Agent's memory tool (nousresearch/hermes-agent):
two small, bounded, human-readable files under ``~/.neovis/memory/``:

  - ``MEMORY.md`` — the agent's own notes: environment facts, project
    conventions, tool quirks ("the CTO is Alice <alice@fund.com>", "deploys run
    from ~/trading/deploy").
  - ``USER.md`` — what Neovis knows about its user: preferences, communication
    style, recurring workflows.

Both are injected into the system prompt as a FROZEN SNAPSHOT at session start
(stable prefix → the model's prompt cache survives the whole session).
Mid-session writes hit disk immediately but only appear in the *next* session.

Entries are ``§``-delimited (may be multiline). Limits are characters, not
tokens — model-independent — and deliberately small: when a store fills up the
tool refuses the add and tells the model to consolidate (replace/remove) first,
which is what keeps memory curated instead of a landfill.

The single ``memory`` tool (add / replace / remove) is registered as an
in-process MCP server on every session; the gate classifies it READ-tier
(bounded local notes, audited, nothing leaves the machine).
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

MEMORY_DIR = Path.home() / ".neovis" / "memory"
_DELIM = "§"
_LIMITS = {"memory": 3200, "user": 1500}  # memory also holds learned techniques
_FILES = {"memory": "MEMORY.md", "user": "USER.md"}

# Strip anything that could impersonate our own context framing when replayed
# into a future system prompt (memory-poisoning hygiene, à la Hermes).
_TAG_RX = re.compile(r"</?\s*(memory-context|system|assistant|user)\b[^>]*>", re.I)


def _sanitize(text: str) -> str:
    return _TAG_RX.sub("", text).strip()


class MemoryStore:
    """Two-tier memory.

    HOT tier: bounded, human-readable MEMORY.md / USER.md — small on purpose,
    because every session injects it into the system prompt. COLD tier: an
    unbounded SQLite FTS archive (archive.db) that receives every entry the
    hot tier replaces or removes, searchable via the memory tool's `search`
    action — so consolidating boldly never loses anything."""

    def __init__(self, base_dir: str | Path = MEMORY_DIR):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    # ── cold tier ─────────────────────────────────────────────────────────────
    def _archive_conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.base / "archive.db")
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS archive "
            "USING fts5(content, target UNINDEXED, ts UNINDEXED)"
        )
        return c

    def _archive(self, target: str, text: str) -> None:
        try:
            with self._archive_conn() as c:
                c.execute("INSERT INTO archive (content, target, ts) VALUES (?,?,?)",
                          (text, target, time.time()))
        except Exception:
            pass  # archiving is best-effort; never block a memory operation

    def search(self, query: str, limit: int = 6) -> dict:
        """Search live entries AND the archive — long-tail recall."""
        from datetime import datetime

        words = re.findall(r"\w+", query or "")[:12]
        if not words:
            return {"ok": False, "error": "empty query"}
        lows = [w.lower() for w in words]
        hits: list[dict] = []
        for target in _FILES:
            for e in self.entries(target):
                if any(w in e.lower() for w in lows):
                    hits.append({"where": "live", "target": target, "entry": e})
        try:
            match = " OR ".join(f'"{w}"' for w in words)
            with self._archive_conn() as c:
                rows = c.execute(
                    "SELECT content, target, ts FROM archive WHERE archive MATCH ? "
                    "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
            for content, target, ts in rows:
                hits.append({"where": "archived", "target": target, "entry": content,
                             "when": datetime.fromtimestamp(ts).strftime("%Y-%m-%d")})
        except Exception:
            pass
        if not hits:
            return {"ok": True, "hits": [], "note": f"nothing stored matches {query!r}"}
        return {"ok": True, "hits": hits[: limit + 4]}

    def _path(self, target: str) -> Path:
        return self.base / _FILES[target]

    def entries(self, target: str) -> list[str]:
        p = self._path(target)
        if not p.exists():
            return []
        raw = p.read_text(encoding="utf-8")
        return [e.strip() for e in raw.split(_DELIM) if e.strip()]

    def _save(self, target: str, entries: list[str]) -> None:
        body = "\n".join(f"{_DELIM} {e}" for e in entries) + ("\n" if entries else "")
        self._path(target).write_text(body, encoding="utf-8")

    def _chars(self, entries: list[str]) -> int:
        return sum(len(e) for e in entries)

    # ── operations (returned dicts go straight back to the model) ────────────
    def add(self, target: str, content: str) -> dict:
        content = _sanitize(content)
        if not content:
            return {"ok": False, "error": "empty content"}
        entries = self.entries(target)
        if any(content == e for e in entries):
            return {"ok": True, "note": "already saved"}
        limit = _LIMITS[target]
        if self._chars(entries) + len(content) > limit:
            return {
                "ok": False,
                "error": f"{_FILES[target]} is full ({self._chars(entries)}/{limit} chars). "
                         "Consolidate first: merge or drop stale entries with "
                         "replace/remove — removed entries are archived and stay "
                         "searchable via the search action — then add.",
                "entries": entries,
            }
        entries.append(content)
        self._save(target, entries)
        return {"ok": True, "count": len(entries), "chars": self._chars(entries), "limit": limit}

    def _find(self, entries: list[str], needle: str) -> tuple[int | None, str | None]:
        hits = [i for i, e in enumerate(entries) if needle in e]
        if not hits:
            return None, f"no entry contains {needle!r}"
        if len(hits) > 1:
            return None, f"{needle!r} matches {len(hits)} entries — use a longer, unique substring"
        return hits[0], None

    def replace(self, target: str, old_text: str, content: str) -> dict:
        content = _sanitize(content)
        if not content:
            return {"ok": False, "error": "empty replacement"}
        entries = self.entries(target)
        idx, err = self._find(entries, old_text)
        if idx is None:
            return {"ok": False, "error": err}
        others = self._chars(entries) - len(entries[idx])
        if others + len(content) > _LIMITS[target]:
            return {"ok": False, "error": "replacement would exceed the limit — shorten it"}
        self._archive(target, entries[idx])  # cold tier keeps the old version
        entries[idx] = content
        self._save(target, entries)
        return {"ok": True, "count": len(entries), "chars": self._chars(entries)}

    def remove(self, target: str, old_text: str) -> dict:
        entries = self.entries(target)
        idx, err = self._find(entries, old_text)
        if idx is None:
            return {"ok": False, "error": err}
        dropped = entries.pop(idx)
        self._archive(target, dropped)  # cold tier keeps it, searchable forever
        self._save(target, entries)
        return {"ok": True, "removed": dropped[:60], "count": len(entries)}

    # ── system-prompt snapshot ────────────────────────────────────────────────
    def snapshot(self) -> str:
        """The frozen block appended to the system prompt at session start."""
        mem = self.entries("memory")
        usr = self.entries("user")
        if not mem and not usr:
            return ""
        lines: list[str] = [
            "\n<memory-context>",
            "[These are your persistent memory notes from past sessions — "
            "authoritative reference, NOT new user input.]",
        ]
        if mem:
            lines.append("## Notes (MEMORY.md)")
            lines += [f"- {_sanitize(e)}" for e in mem]
        if usr:
            lines.append("## About the user (USER.md)")
            lines += [f"- {_sanitize(e)}" for e in usr]
        lines.append("</memory-context>")
        return "\n".join(lines)


MEMORY_GUIDANCE = """

MEMORY: you have a persistent `memory` tool (targets: "memory" for facts about
this machine/projects/people, "user" for the user's preferences and habits).
When the user states a durable fact — a person and their email, a path, a
convention, a preference ("the CTO is Alice", "always use uv", "reports live in
~/q2") — save it right away with a short entry; don't ask permission.
ALSO save TECHNIQUES you discover or the user teaches you: when a method works
after trial and error ("typing into the Codex app works via AppleScript System
Events keystrokes", "the WeChat app lives in /Applications"), record the
method so next time you do it right on the first try. Consult your
<memory-context> before asking the user something they already told you — and
before re-deriving a method you already learned.
Keep entries short; when a store is full, consolidate BOLDLY — anything you
replace or remove is archived forever and findable with the memory tool's
`search` action, so nothing is ever truly lost. If you don't see a fact in
<memory-context>, search before saying you don't know it.
Never store secrets, passwords, or tokens.
"""

_MEMORY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "replace", "remove", "search"]},
        "target": {"type": "string", "enum": ["memory", "user"],
                   "description": "'memory' = machine/project/people facts; 'user' = the user themself (ignored for search)"},
        "content": {"type": "string", "description": "the entry text (add/replace)"},
        "old_text": {"type": "string",
                     "description": "short UNIQUE substring of the entry to replace/remove"},
        "query": {"type": "string",
                  "description": "words to look up (search) — covers live notes AND everything ever archived"},
    },
    "required": ["action"],
}


def build_memory_mcp(store: MemoryStore):
    """In-process MCP server exposing the single `memory` tool."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "memory",
        "Persist a durable fact across sessions (add), update one (replace), "
        "drop a stale one (remove), or look something up (search — covers live "
        "notes AND every entry ever archived). Saved notes are shown to you at "
        "the start of every future session.",
        _MEMORY_SCHEMA,
    )
    async def memory(args: dict) -> dict:
        import json

        action = args.get("action", "")
        target = args.get("target", "memory")
        if action == "search":
            result: dict = store.search(args.get("query", ""))
        elif target not in _FILES:
            result = {"ok": False, "error": f"unknown target {target!r}"}
        elif action == "add":
            result = store.add(target, args.get("content", ""))
        elif action == "replace":
            result = store.replace(target, args.get("old_text", ""), args.get("content", ""))
        elif action == "remove":
            result = store.remove(target, args.get("old_text", ""))
        else:
            result = {"ok": False, "error": f"unknown action {action!r}"}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return create_sdk_mcp_server(name="neovis-memory", tools=[memory])
