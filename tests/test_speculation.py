"""Speculative turns must stay SILENT until the router rules on them, and a
filler must only cover delegated work — not every sentence."""

from __future__ import annotations

import asyncio

from neovis.channels.desktop.voice import VoiceLoop


def bare() -> VoiceLoop:
    lp = VoiceLoop.__new__(VoiceLoop)
    lp._speech_q = asyncio.Queue()
    lp._last_spoken = ""
    lp._proc = None
    lp._speaking_item = None
    lp._speak_end = 0.0
    lp._narrated = ""
    lp._speculating = False
    lp._held_narration = ""
    lp._trace = None
    lp.ui = type("UI", (), {"step": lambda self, x: None})()
    return lp


def queued(lp) -> list[str]:
    return [lp._speech_q.get_nowait()["text"] for _ in range(lp._speech_q.qsize())]


def test_speculative_narration_is_withheld():
    lp = bare()
    lp._speculating = True
    lp._narrate("Opening VS Code on the repo now.")
    assert lp._speech_q.empty()                       # nothing spoken yet
    assert lp._held_narration == "Opening VS Code on the repo now."


def test_confirmed_task_releases_the_held_line():
    lp = bare()
    lp._speculating = True
    lp._narrate("Checking the repository.")
    lp._narrate("Found 12 files.")                    # freshest wins
    lp._release_speculation()
    assert queued(lp) == ["Found 12 files."]
    assert not lp._speculating


def test_discarded_speculation_is_never_heard():
    """This is the duplicate-speech bug: Haiku says 'chat', so the main model's
    words were never meant for the user."""
    lp = bare()
    lp._speculating = True
    lp._narrate("I'll be here whenever you need me.")
    lp._discard_speculation()
    assert lp._speech_q.empty()
    assert lp._narrated == "" and lp._held_narration == ""


def test_narration_speaks_immediately_once_confirmed():
    lp = bare()
    lp._speculating = True
    lp._release_speculation()          # router said "task", nothing held yet
    lp._narrate("Opening VS Code now.")
    assert queued(lp) == ["Opening VS Code now."]


# ── the filler only covers delegated work ─────────────────────────────────────
def test_filler_fires_for_real_requests():
    for line in ("Open VS Code on the repo",
                 "Count the files in my downloads",
                 "Delete the old backups"):
        assert VoiceLoop._ack_bucket(line) != "ack", line


def test_no_filler_for_musings():
    # GPT-Live's rule: no butler announcement when nothing is being delegated
    for line in ("I guess you should take a rest then",
                 "Yes, it's good, it's good",
                 "That was pretty impressive honestly"):
        assert VoiceLoop._ack_bucket(line) == "ack", line
