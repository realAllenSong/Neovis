"""The voice-switch easter egg: parsing accent/gender/name commands."""

import pytest

from neovis.voice.commands import parse_voice_command
from neovis.voice.tts import VOICES, resolve_voice


def _resolve(intent):
    return resolve_voice(name=intent.name, accent=intent.accent, gender=intent.gender)


@pytest.mark.parametrize("text,expected_voice", [
    ("switch to a British male accent", "george"),
    ("talk like an American woman", "sky"),
    ("change to a british female voice", "emma"),
    ("use an american male voice", "adam"),
    ("use the emma voice", "emma"),
    ("switch to george", "george"),
])
def test_fully_specified_commands_resolve(text, expected_voice):
    intent = parse_voice_command(text)
    assert intent is not None and intent.specified
    sid, name = _resolve(intent)
    assert name == expected_voice
    assert sid == VOICES[expected_voice][0]


@pytest.mark.parametrize("text,accent,gender", [
    ("make it British", "british", None),   # gender to be filled from current voice
    ("switch to a male voice", None, "male"),
    ("use a woman's voice", None, "female"),
])
def test_partial_commands_capture_what_was_said(text, accent, gender):
    intent = parse_voice_command(text)
    assert intent is not None
    assert intent.accent == accent and intent.gender == gender


def test_bare_change_request_is_underspecified():
    intent = parse_voice_command("change your voice")
    assert intent is not None and not intent.specified  # caller should ask


@pytest.mark.parametrize("text", [
    "what is the weather today",
    "send an email to my colleague",
    "open my downloads folder",
])
def test_non_voice_commands_ignored(text):
    assert parse_voice_command(text) is None
