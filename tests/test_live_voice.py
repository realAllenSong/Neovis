"""GPT-Live-inspired pieces: endpointing heuristic + instant clip bank."""

from __future__ import annotations

from pathlib import Path

from neovis.channels.desktop.voice import VoiceLoop, _utterance_complete


# ── endpointing: hold mid-thought pauses open ─────────────────────────────────
def test_finished_thoughts_dispatch():
    assert _utterance_complete("Open the quarterly report folder.")
    assert _utterance_complete("What's the current git branch?")
    assert _utterance_complete("Stop.")          # short commands go immediately
    assert _utterance_complete("Yes")


def test_mid_thought_holds():
    assert not _utterance_complete("Check the downloads folder and")
    assert not _utterance_complete("I want you to open, um")
    assert not _utterance_complete("Take a screenshot of the")
    assert not _utterance_complete("Email the report to")           # no terminal punct
    assert not _utterance_complete("Move the files into the folder")  # trails off


def test_empty_is_incomplete():
    assert not _utterance_complete("")
    assert not _utterance_complete("   ")


# ── instant clip bank ─────────────────────────────────────────────────────────
class _StubTTS:
    voice_name = "stubtest"

    def synthesize(self, text, path):
        Path(path).write_bytes(b"RIFF0000WAVE")
        return str(path), 0.1


def _bare_loop() -> VoiceLoop:
    lp = VoiceLoop.__new__(VoiceLoop)
    lp.tts = _StubTTS()
    lp._bank = {}
    lp._last_spoken = ""
    lp._playing = None
    lp._speak_end = 0.0
    return lp


def test_bank_builds_all_kinds(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    lp = _bare_loop()
    lp.build_ack_bank()
    assert set(lp._bank) == {"ack", "hold", "stop"}
    for clips in lp._bank.values():
        assert clips and all(Path(w).exists() for _, w in clips)


def test_play_cached_updates_echo_reference(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    lp = _bare_loop()
    lp.build_ack_bank()
    lp._play_file = lambda wav, blocking: None  # don't actually play
    lp.play_cached("stop")
    assert "Stopping." in lp._last_spoken  # echo filter knows what we said
