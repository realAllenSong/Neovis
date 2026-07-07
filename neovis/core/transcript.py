"""Conversation recall — "what did I ask you yesterday?"

Every turn that flows through :meth:`NeovisSession.send` (Slack, voice, REPL)
is recorded into one global SQLite **FTS5** table at ``~/.neovis/transcripts.db``.
The in-process ``recall`` MCP tool searches it — pure BM25, zero LLM cost
(pattern from Hermes Agent's session_search_tool):

  - with ``query``  → top matching turns, each with a ±2-message window of the
    surrounding conversation for context
  - without        → the most recent conversations, newest first

The gate classifies recall READ-tier: it only reads the user's own history.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / ".neovis" / "transcripts.db"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS transcript USING fts5(
    content, session_id UNINDEXED, actor UNINDEXED, role UNINDEXED, ts UNINDEXED
);
"""


def _when(ts: float) -> str:
    d = datetime.fromtimestamp(float(ts))
    days = (datetime.now().date() - d.date()).days
    rel = "today" if days == 0 else ("yesterday" if days == 1 else f"{days}d ago")
    return f"{d:%Y-%m-%d %H:%M} ({rel})"


class ConversationLog:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, session_id: str, actor: str, role: str, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        with self._conn() as c:
            c.execute(
                "INSERT INTO transcript (content, session_id, actor, role, ts) VALUES (?,?,?,?,?)",
                (content[:4000], session_id, actor, role, time.time()),
            )

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """BM25 hits, each with a small window of surrounding turns."""
        words = re.findall(r"\w+", query or "")[:12]
        if not words:
            return []
        match = " OR ".join(f'"{w}"' for w in words)
        with self._conn() as c:
            hits = c.execute(
                "SELECT rowid, session_id, role, ts, snippet(transcript, 0, '«', '»', '…', 16) "
                "FROM transcript WHERE transcript MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()
            out = []
            for rowid, session_id, role, ts, snip in hits:
                window = c.execute(
                    "SELECT role, substr(content, 1, 160) FROM transcript "
                    "WHERE session_id = ? AND rowid BETWEEN ? AND ? ORDER BY rowid",
                    (session_id, rowid - 2, rowid + 2),
                ).fetchall()
                out.append({
                    "when": _when(ts), "session": session_id, "role": role,
                    "snippet": snip,
                    "context": [f"{r}: {t}" for r, t in window],
                })
            return out

    def recent(self, limit: int = 8) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT session_id, ts, substr(content, 1, 120) FROM transcript "
                "WHERE role = 'user' ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"when": _when(ts), "session": s, "asked": t} for s, ts, t in rows]


_RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string",
                  "description": "words to search past conversations for; omit to browse recent turns"},
        "limit": {"type": "integer", "description": "max results (default 5)"},
    },
}


def build_recall_mcp(log: ConversationLog):
    """In-process MCP server exposing the single `recall` tool."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        "recall",
        "Search your past conversations with this user (all channels — Slack, "
        "voice, terminal). Use when they reference something from before: "
        "'what did I ask yesterday', 'that file we made', 'like last time'.",
        _RECALL_SCHEMA,
    )
    async def recall(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        limit = int(args.get("limit") or 5)
        try:
            if query:
                rows = log.search(query, limit=limit)
                if not rows:
                    text = f"No past conversation matches {query!r}."
                else:
                    parts = []
                    for r in rows:
                        ctx = "\n    ".join(r["context"])
                        parts.append(f"[{r['when']}] ({r['session']}) {r['role']}: {r['snippet']}\n    {ctx}")
                    text = "\n\n".join(parts)
            else:
                rows = log.recent(limit=limit)
                text = "\n".join(f"[{r['when']}] ({r['session']}) user: {r['asked']}" for r in rows) \
                       or "No conversation history yet."
        except Exception as exc:
            text = f"recall failed: {exc}"
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="neovis-recall", tools=[recall])
