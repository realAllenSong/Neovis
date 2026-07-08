"""Memory → hotwords: name extraction and fuzzy transcript correction."""

from __future__ import annotations

from neovis.core.memory import MemoryStore
from neovis.voice.hotwords import correct_transcript, names_from_memory


def store(tmp_path) -> MemoryStore:
    return MemoryStore(base_dir=tmp_path / "memory")


def test_names_extracted_from_memory(tmp_path):
    s = store(tmp_path)
    s.add("memory", "CTO is Alice Zhang — alice.zhang@fund.com")
    s.add("memory", "Watch the NVDA position with Zhiyuan Song")
    s.add("user", "Daily standup is at 9:30am.")
    names = names_from_memory(s)
    assert "Alice Zhang" in names and "Zhiyuan Song" in names and "NVDA" in names
    # roles/dates don't leak in
    assert "CTO" not in names and "Daily" not in names
    # multi-word names sort before fragments
    assert names.index("Alice Zhang") < names.index("NVDA")


def test_names_empty_memory(tmp_path):
    assert names_from_memory(store(tmp_path)) == []


def test_correct_snaps_near_miss():
    out = correct_transcript("Ask Alice Jang to review the position.", ["Alice Zhang"])
    assert out == "Ask Alice Zhang to review the position."


def test_correct_fixes_casing_and_keeps_punctuation():
    out = correct_transcript("email alice zhang, then ping me", ["Alice Zhang"])
    assert out == "email Alice Zhang, then ping me"


def test_correct_leaves_distant_words_alone():
    text = "Ask Bob Miller to review the position."
    assert correct_transcript(text, ["Alice Zhang"]) == text


def test_correct_no_phrases_or_empty():
    assert correct_transcript("hello", []) == "hello"
    assert correct_transcript("", ["Alice Zhang"]) == ""


def test_correct_multiword_wins_over_fragment():
    out = correct_transcript("please email jiyuan song now", ["Zhiyuan Song", "Song"])
    assert "Zhiyuan Song" in out
