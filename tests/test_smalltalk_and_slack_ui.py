"""Small talk must not trigger a butler announcement; Slack must look alive."""

from __future__ import annotations

from neovis.channels.desktop.voice import _SPEECH_RMS_FLOOR, _is_smalltalk
from neovis.channels.slack.app import _shimmer, thinking_frame


# ── small talk gets no ack, no speculative turn ───────────────────────────────
def test_pure_conversation_is_smalltalk():
    for line in ("How are you doing?", "Hey", "hi there", "What's up?",
                 "How's it going", "Who are you?", "thanks, that's perfect",
                 "Nice work", "Are you there?", "Good morning"):
        assert _is_smalltalk(line), line


def test_task_hiding_behind_a_greeting_is_not_smalltalk():
    # these open socially but ask for real work — they must still get an ack
    for line in ("Hey, open Chrome for me",
                 "Hi — delete the old backups",
                 "Good morning, create a file called notes.txt"):
        assert not _is_smalltalk(line), line


def test_plain_requests_are_not_smalltalk():
    for line in ("Count the files in my downloads folder",
                 "What's the current git branch?",
                 "Email Alice the report"):
        assert not _is_smalltalk(line), line


def test_rms_floor_is_meaningful():
    assert 0 < _SPEECH_RMS_FLOOR < 0.05  # quiet room stays quiet, speech passes


# ── the Slack thinking animation ──────────────────────────────────────────────
def test_shimmer_travels_and_keeps_width():
    frames = [_shimmer(i) for i in range(6)]
    assert all(len(f) == 12 for f in frames)
    assert len(set(frames)) == 6              # every frame differs → it moves
    assert all("█" in f for f in frames)      # the bright head is always present


def test_thinking_frame_animates_and_shows_steps():
    a, b = thinking_frame(0), thinking_frame(1)
    assert "Neovis is thinking" in a and a != b   # spinner + shimmer advance
    with_steps = thinking_frame(2, ["Bash `ls`", "Read `notes.txt`"])
    assert "Neovis is thinking" in with_steps
    assert "• Bash `ls`" in with_steps and "• Read `notes.txt`" in with_steps


def test_thinking_frame_caps_the_trace():
    many = [f"step {i}" for i in range(20)]
    out = thinking_frame(0, many)
    assert out.count("•") == 8 and "step 19" in out and "step 5" not in out
