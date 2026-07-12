"""Self-echo detection: Neovis must not chat with itself through the speakers."""

from __future__ import annotations

import time

from neovis.channels.desktop.voice import VoiceLoop


def make_loop() -> VoiceLoop:
    # No session/asr/tts needed for echo logic — bypass __init__.
    loop = VoiceLoop.__new__(VoiceLoop)
    loop._proc = None
    loop._speaking_item = None
    loop._last_spoken = ""
    loop._speak_end = 0.0
    return loop


def test_exact_fragment_of_last_spoken_is_echo():
    lp = make_loop()
    lp._last_spoken = "Hey! What can I help you with? Ready when you are."
    lp._speak_end = time.time()
    assert lp._is_own_echo("Hey.")
    assert lp._is_own_echo("What can I help you with?")


def test_near_match_is_echo():
    lp = make_loop()
    lp._last_spoken = "The current branch is main."
    lp._speak_end = time.time()
    assert lp._is_own_echo("the current branch is maine")


def test_fresh_user_speech_is_not_echo():
    lp = make_loop()
    lp._last_spoken = "Hey! What can I help you with?"
    lp._speak_end = time.time()
    assert not lp._is_own_echo("Open the quarterly report folder.")


def test_old_playback_never_matches():
    lp = make_loop()
    lp._last_spoken = "Hey! What can I help you with?"
    lp._speak_end = time.time() - 10  # playback long over
    assert not lp._is_own_echo("Hey.")


def test_empty_cases():
    lp = make_loop()
    assert not lp._is_own_echo("")
    lp._last_spoken = ""
    lp._speak_end = time.time()
    assert not lp._is_own_echo("Hey.")
