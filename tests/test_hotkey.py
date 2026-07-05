"""The macOS hotkey listener's press/release logic — pure, no Quartz needed.

This is the pynput replacement that avoids the TIS-off-main-thread SIGTRAP
when running alongside Qt; the keycode/flag matching must be exact.
"""

from __future__ import annotations

import pytest

from neovis.channels.desktop.hotkey import (
    _DARWIN_KEYS,
    _FLAGS_CHANGED,
    _KEY_DOWN,
    _KEY_UP,
    _DarwinHotkey,
)

CMD_R_CODE, CMD_R_FLAG = _DARWIN_KEYS["cmd_r"]
CMD_L_CODE, CMD_L_FLAG = _DARWIN_KEYS["cmd_l"]
F5_CODE, _ = _DARWIN_KEYS["f5"]


def make(key="cmd_r"):
    events: list[str] = []
    hk = _DarwinHotkey(key, lambda: events.append("press"), lambda: events.append("release"))
    return hk, events


def test_gui_hotkeys_all_mapped():
    # every key offered in the app's dropdown must resolve
    for name in ("cmd_r", "cmd_l", "alt_r", "ctrl_r", "f5", "shift_r"):
        assert name in _DARWIN_KEYS


def test_unknown_key_rejected():
    with pytest.raises(ValueError):
        _DarwinHotkey("bogus", lambda: None, lambda: None)


def test_modifier_press_release():
    hk, events = make("cmd_r")
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, CMD_R_FLAG, False)   # down
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, 0, False)            # up
    assert events == ["press", "release"]


def test_other_keys_ignored():
    hk, events = make("cmd_r")
    hk._handle(_FLAGS_CHANGED, CMD_L_CODE, CMD_L_FLAG, False)   # LEFT cmd, not ours
    hk._handle(_KEY_DOWN, 0x00, 0, False)                       # letter key
    assert events == []


def test_left_cmd_held_does_not_fake_release():
    """Release right-cmd while LEFT cmd is still held: the generic command bit
    stays set, but the device-specific right-cmd bit clears — must release."""
    hk, events = make("cmd_r")
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, CMD_R_FLAG | CMD_L_FLAG, False)  # both down
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, CMD_L_FLAG, False)               # right up, left held
    assert events == ["press", "release"]


def test_no_double_press():
    hk, events = make("cmd_r")
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, CMD_R_FLAG, False)
    hk._handle(_FLAGS_CHANGED, CMD_R_CODE, CMD_R_FLAG, False)   # duplicate state
    assert events == ["press"]


def test_f5_uses_key_events_and_ignores_autorepeat():
    hk, events = make("f5")
    hk._handle(_KEY_DOWN, F5_CODE, 0, False)
    hk._handle(_KEY_DOWN, F5_CODE, 0, True)    # autorepeat while held
    hk._handle(_KEY_DOWN, F5_CODE, 0, True)
    hk._handle(_KEY_UP, F5_CODE, 0, False)
    assert events == ["press", "release"]


def test_f5_ignores_flags_changed():
    hk, events = make("f5")
    hk._handle(_FLAGS_CHANGED, F5_CODE, 0xFFFF, False)
    assert events == []
