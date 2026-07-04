"""Audit log — the tamper-evident record of everything the agent did.

Every tool invocation is written here: what was called, with which arguments,
the risk tier, who approved it, the outcome. This is the compliance story that
separates Neovis from a hobbyist "run shell from chat" bot. SQLite keeps it
zero-ops; a deploy can mirror rows to a #jarvis-audit channel.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    session_id  TEXT,
    actor       TEXT,           -- who issued the request (slack user / "cli")
    tool        TEXT NOT NULL,
    args        TEXT,           -- json
    risk        TEXT NOT NULL,
    status      TEXT NOT NULL,  -- ok | error | denied | rejected
    approver    TEXT,           -- who approved a dangerous action
    result      TEXT            -- truncated result / error
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit(session_id);
"""

_RESULT_CAP = 2000


@dataclass
class AuditRecord:
    tool: str
    risk: str
    status: str
    args: dict[str, Any] | None = None
    session_id: str | None = None
    actor: str | None = None
    approver: str | None = None
    result: str | None = None


class AuditLog:
    """Thin SQLite wrapper. Safe to share across threads (check_same_thread off)."""

    def __init__(self, db_path: str | Path = "neovis_audit.db"):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, rec: AuditRecord) -> int:
        result = rec.result
        if result is not None and len(result) > _RESULT_CAP:
            result = result[:_RESULT_CAP] + f"… [+{len(result) - _RESULT_CAP} chars]"
        cur = self._conn.execute(
            """INSERT INTO audit
               (ts, session_id, actor, tool, args, risk, status, approver, result)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                rec.session_id,
                rec.actor,
                rec.tool,
                json.dumps(rec.args, default=str) if rec.args is not None else None,
                rec.risk,
                rec.status,
                rec.approver,
                result,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT ts, actor, tool, risk, status, approver, result "
            "FROM audit ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
