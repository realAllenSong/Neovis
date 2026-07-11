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


def test_repertoire_is_wide():
    lines = VoiceLoop._BANK_LINES
    all_acks = [l for k, v in lines.items() if k.startswith("ack") for l in v]
    assert len(all_acks) >= 40          # variety sells the illusion
    assert len(set(all_acks)) == len(all_acks)  # no duplicates across buckets
    assert len(lines["hold"]) >= 10
    assert all(len(l.split()) <= 8 for kind in lines.values() for l in kind)


def test_ack_bucket_heuristic():
    b = VoiceLoop._ack_bucket
    assert b("How many python files are in my repo?") == "ack_look"
    assert b("Open the quarterly report folder for me") == "ack_do"
    assert b("Delete the old backups in downloads") == "ack_careful"
    assert b("Send an email to Alice about the meeting") == "ack_careful"
    # careful outranks look: mixed intent gets the cautious ack
    assert b("Check the folder and delete the duplicates") == "ack_careful"
    assert b("Please summarize everything for me somehow") == "ack"  # no keywords
    assert b("") == "ack"


def test_bank_seeds_all_kinds_instantly(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceLoop, "BANK_CACHE", tmp_path)
    lp = _bare_loop()
    lp.build_ack_bank()   # no event loop → seeds only, background fill skipped
    assert set(lp._bank) == set(VoiceLoop._BANK_LINES)  # every kind + bucket
    for clips in lp._bank.values():
        assert clips and all(Path(w).exists() for _, w in clips)


def test_bank_reloads_from_disk_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceLoop, "BANK_CACHE", tmp_path)
    lp = _bare_loop()
    lp.build_ack_bank()
    synth_calls = []
    lp.tts.synthesize = lambda t, p: synth_calls.append(t)  # would fail if used
    lp.build_ack_bank()   # second boot: everything seeded loads from disk
    assert not synth_calls
    assert all(lp._bank[k] for k in lp._bank)


def test_play_cached_avoids_recent_repeats_and_marks_echo(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceLoop, "BANK_CACHE", tmp_path)
    lp = _bare_loop()
    # pre-populate the full stop repertoire so rotation has room
    for line in VoiceLoop._BANK_LINES["stop"]:
        p = lp._bank_path("stop", line)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF0000WAVE")
    lp.build_ack_bank()
    lp._play_file = lambda wav, blocking: None  # don't actually play
    played = []
    for _ in range(6):
        before = lp._last_spoken
        lp.play_cached("stop")
        played.append(lp._last_spoken[len(before):].strip())
    assert len(set(played)) == len(played)  # 6 clips available → no repeat
    assert any("topp" in p or "ancel" in p or "ropping" in p for p in played)
    assert lp._last_spoken  # echo filter knows what we said
