"""ASR engine selection: explicit choice wins when installed, auto prefers
speed, graceful fallback chain."""

from __future__ import annotations

from neovis.voice.asr import _select_engine


def test_auto_prefers_parakeet():
    assert _select_engine("auto", parakeet=True, qwen3=True) == "parakeet"
    assert _select_engine("auto", parakeet=True, qwen3=False) == "parakeet"


def test_auto_falls_back():
    assert _select_engine("auto", parakeet=False, qwen3=True) == "qwen3"
    assert _select_engine("auto", parakeet=False, qwen3=False) == "zipformer"


def test_explicit_choice_wins_when_installed():
    assert _select_engine("qwen3", parakeet=True, qwen3=True) == "qwen3"
    assert _select_engine("parakeet", parakeet=True, qwen3=True) == "parakeet"


def test_explicit_choice_missing_model_degrades():
    assert _select_engine("qwen3", parakeet=True, qwen3=False) == "parakeet"
    assert _select_engine("parakeet", parakeet=False, qwen3=True) == "qwen3"
