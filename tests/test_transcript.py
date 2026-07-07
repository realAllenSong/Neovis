"""Conversation recall (FTS5) + steer contract + gate tiers."""

from __future__ import annotations

import sqlite3

import pytest

from neovis.core.config import PolicyConfig
from neovis.core.gate import Consequence, classify
from neovis.core.transcript import ConversationLog


def _fts5_available() -> bool:
    try:
        sqlite3.connect(":memory:").execute(
            "CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _fts5_available(), reason="sqlite without FTS5")


def log(tmp_path) -> ConversationLog:
    return ConversationLog(db_path=tmp_path / "transcripts.db")


def test_record_and_search(tmp_path):
    lg = log(tmp_path)
    lg.record("slack:U1", "U1", "user", "please organize the Q2 files in Downloads")
    lg.record("slack:U1", "U1", "assistant", "Done — moved 14 Q2 files into ~/q2")
    lg.record("desktop-voice", "voice", "user", "take a screenshot")
    hits = lg.search("Q2 files")
    assert hits and "Q2" in hits[0]["snippet"]
    # the hit carries surrounding context from the same session only
    assert any("organize" in c or "moved 14" in c for c in hits[0]["context"])
    assert all("screenshot" not in c for c in hits[0]["context"])


def test_search_no_match_and_empty_query(tmp_path):
    lg = log(tmp_path)
    lg.record("s", "a", "user", "hello world")
    assert lg.search("zebra quantum") == []
    assert lg.search("") == []


def test_search_survives_fts_special_chars(tmp_path):
    lg = log(tmp_path)
    lg.record("s", "a", "user", "email the CTO about x")
    # raw quotes/operators would be an FTS5 syntax error if unsanitized
    assert lg.search('email "CTO" AND (x OR *)') != []


def test_recent_lists_user_turns_newest_first(tmp_path):
    lg = log(tmp_path)
    for i in range(3):
        lg.record("s", "a", "user", f"request number {i}")
        lg.record("s", "a", "assistant", f"reply {i}")
    recent = lg.recent(limit=2)
    assert len(recent) == 2
    assert "number 2" in recent[0]["asked"] and "number 1" in recent[1]["asked"]


def test_empty_content_not_recorded(tmp_path):
    lg = log(tmp_path)
    lg.record("s", "a", "user", "   ")
    assert lg.recent() == []


def test_gate_classifies_recall_as_read():
    tier, _ = classify("mcp__neovis-recall__recall", {"query": "q2"}, PolicyConfig())
    assert tier is Consequence.READ


def test_steer_note_shape():
    from neovis.channels.slack.app import _STEER_NOTE

    assert "mid-task" in _STEER_NOTE and _STEER_NOTE.endswith("\n")
