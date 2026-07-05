"""User settings at ~/.neovis/settings.yaml — shared by the GUI and the voice loop.

Holds the few things a user changes: the push-to-talk hotkey, the voice, ASR
hotwords, and the Slack tokens. The GUI reads and writes this file; nothing here
requires the terminal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

SETTINGS_PATH = Path.home() / ".neovis" / "settings.yaml"

DEFAULTS: dict[str, Any] = {
    "voice_enabled": True,         # run the desktop push-to-talk voice loop
    "hotkey": "cmd_r",             # push-to-talk key
    "voice": "sky",                # sky / adam / emma / george
    "hands_free": False,           # continuous VAD listening instead of hold-to-talk
    "barge_in": False,             # interrupt Neovis by talking (headphones only)
    "hotwords": [],                # ASR contextual-bias words
    "slack_bot_token": "",         # xoxb-…
    "slack_app_token": "",         # xapp-…
}


def load() -> dict[str, Any]:
    data = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            data.update(yaml.safe_load(SETTINGS_PATH.read_text()) or {})
        except Exception:
            pass
    return data


def save(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULTS, **settings}
    SETTINGS_PATH.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))
