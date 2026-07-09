"""Progressive narration + white-box trace: the pieces that turn silent
working time into a narrated JARVIS conversation."""

from __future__ import annotations

import json

from neovis.channels.desktop.voice import _first_sentence, _Trace


def test_first_sentence_basic():
    assert _first_sentence("Checking the repository now. This may take a bit.") == \
        "Checking the repository now."


def test_first_sentence_strips_markdown():
    assert _first_sentence("**Done!** `git` says main.\nMore lines.") == "Done!"
    assert _first_sentence("`git` says main today.\nMore.") == "git says main today."


def test_first_sentence_newline_stop_and_cap():
    assert _first_sentence("Let me look\nrest") == "Let me look"
    assert len(_first_sentence("word " * 100)) <= 140
    assert _first_sentence("") == ""


def test_trace_writes_jsonl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(_Trace, "PATH", tmp_path / "voice_trace.jsonl")
    tr = _Trace("open wechat", asr_s=0.08)
    tr.mark("router")
    tr.d["action"] = "task"
    tr.mark("turn")
    tr.done(narrated=True)
    line = json.loads((tmp_path / "voice_trace.jsonl").read_text().splitlines()[0])
    assert line["utterance"] == "open wechat"
    assert line["asr_s"] == 0.08 and "router_s" in line and "turn_s" in line
    assert line["narrated"] is True
    out = capsys.readouterr().out
    assert "⏱ [task]" in out and "router=" in out
