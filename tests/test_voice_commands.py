"""The voice-switch easter egg: parsing accent/gender/name commands."""

import pytest

from neovis.voice.commands import parse_voice_command
from neovis.voice.tts import VOICES


@pytest.mark.parametrize("text,expected_voice", [
    ("switch to a British male accent", "george"),
    ("talk like an American woman", "sky"),
    ("change to a british female voice", "emma"),
    ("use an american male voice", "adam"),
    ("use the emma voice", "emma"),
    ("switch to george", "george"),
])
def test_voice_command_matches(text, expected_voice):
    result = parse_voice_command(text)
    assert result is not None
    sid, name = result
    assert name == expected_voice
    assert sid == VOICES[expected_voice][0]


@pytest.mark.parametrize("text", [
    "what is the weather today",
    "send an email to my colleague",
    "open my downloads folder",
])
def test_non_voice_commands_ignored(text):
    assert parse_voice_command(text) is None
