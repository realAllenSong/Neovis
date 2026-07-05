"""The voice interaction contract: spoken/detail reply splitting, Slack
voice-note detection, and the ASR model preference."""

from __future__ import annotations

from neovis.channels.desktop.voice import VOICE_REPLY_NOTES, split_reply
from neovis.channels.slack.app import _audio_files


# ── split_reply: what gets spoken vs shown ────────────────────────────────────
def test_split_protocol():
    spoken, detail = split_reply("Done — WeChat is open.\n---\nOpened /Applications/WeChat.app\nFocused the main window.")
    assert spoken == "Done — WeChat is open."
    assert "Focused the main window." in detail


def test_split_first_line_fallback():
    spoken, detail = split_reply("Here you go.\nLine two\nLine three")
    assert spoken == "Here you go."
    assert detail.startswith("Line two")


def test_split_single_line():
    spoken, detail = split_reply("All set.")
    assert (spoken, detail) == ("All set.", "")


def test_split_strips_markdown_and_never_empty():
    spoken, _ = split_reply("**Done!** `ls` finished.")
    assert "*" not in spoken and "`" not in spoken
    assert split_reply("")[0] == "Done."
    assert split_reply("   \n\n")[0] == "Done."


def test_split_caps_length():
    spoken, _ = split_reply("word " * 400)
    assert len(spoken) <= 240


def test_voice_notes_mention_the_protocol():
    assert "---" in VOICE_REPLY_NOTES and "transcript" in VOICE_REPLY_NOTES


# ── Slack voice notes ─────────────────────────────────────────────────────────
def test_audio_files_detects_voice_memo():
    event = {"files": [
        {"name": "audio_message.m4a", "mimetype": "audio/mp4;codecs=aac", "filetype": "m4a"},
        {"name": "pic.png", "mimetype": "image/png", "filetype": "png"},
    ]}
    hits = _audio_files(event)
    assert len(hits) == 1 and hits[0]["name"] == "audio_message.m4a"


def test_audio_files_by_filetype_only():
    event = {"files": [{"name": "clip.webm", "mimetype": "video/webm", "filetype": "webm"}]}
    assert len(_audio_files(event)) == 1


def test_audio_files_none():
    assert _audio_files({}) == []
    assert _audio_files({"files": [{"name": "doc.pdf", "mimetype": "application/pdf",
                                    "filetype": "pdf"}]}) == []


# ── ASR default prefers Parakeet when installed ───────────────────────────────
def test_asr_prefers_parakeet_when_present():
    from neovis.voice.asr import DEFAULT_ASR_DIR, PARAKEET_DIR

    if PARAKEET_DIR.exists():
        assert DEFAULT_ASR_DIR == PARAKEET_DIR
