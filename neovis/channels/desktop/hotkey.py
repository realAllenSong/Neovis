"""Global push-to-talk key listener.

macOS gets a raw Quartz event tap matched on hardware keycodes — deliberately
NOT pynput: pynput's keyboard listener translates every keycode through
HIToolbox TIS APIs on its listener thread, and modern macOS asserts those are
main-queue-only once an AppKit event loop (our Qt window) is running, killing
the process with SIGTRAP on the first keypress anywhere. (Verified from the
crash report: dispatch_assert_queue → TSMGetInputSourceProperty.) The raw tap
never translates keycodes, so it never touches TIS.

Other platforms keep pynput, which is fine there.

    listener = HotkeyListener("cmd_r", on_press, on_release)
    listener.start()   # raises RuntimeError if the OS won't grant a tap
    ...
    listener.stop()

Callbacks fire on the listener's thread — keep them tiny (set a flag, snapshot
a buffer); never do ASR or I/O in them.
"""

from __future__ import annotations

import sys
import threading

# macOS virtual keycode + device-specific modifier flag bit (distinguishes
# left/right, so holding the *other* cmd key can't confuse press/release).
# F5 is a normal key: no flag bit, tracked via key-down/up events.
_DARWIN_KEYS: dict[str, tuple[int, int | None]] = {
    "cmd_r": (0x36, 0x0010),
    "cmd_l": (0x37, 0x0008),
    "cmd": (0x37, 0x0008),
    "shift_l": (0x38, 0x0002),
    "shift_r": (0x3C, 0x0004),
    "alt_l": (0x3A, 0x0020),
    "alt_r": (0x3D, 0x0040),
    "ctrl_l": (0x3B, 0x0001),
    "ctrl_r": (0x3E, 0x2000),
    "f5": (0x60, None),
}

# CGEventType values (Quartz constants, stable ABI)
_KEY_DOWN, _KEY_UP, _FLAGS_CHANGED = 10, 11, 12

_PERMISSION_HINT = (
    "keyboard permission needed — System Settings → Privacy & Security → "
    "Input Monitoring (or Accessibility): allow this app, then Start again"
)


class _DarwinHotkey:
    """Listen-only CGEventTap on its own CFRunLoop thread. Pure keycode/flag
    matching — no text-input APIs, safe alongside Qt."""

    def __init__(self, key_name: str, on_press, on_release):
        spec = _DARWIN_KEYS.get(key_name)
        if spec is None:
            raise ValueError(f"unknown hotkey {key_name!r} (use one of {sorted(_DARWIN_KEYS)})")
        self._keycode, self._flag = spec
        self._on_press = on_press
        self._on_release = on_release
        self._down = False
        self._tap = None
        self._loop = None
        self._thread: threading.Thread | None = None

    # pure logic, unit-testable without Quartz
    def _handle(self, etype: int, keycode: int, flags: int, repeat: bool) -> None:
        if keycode != self._keycode:
            return
        if self._flag is not None:  # modifier key → flagsChanged carries state
            if etype != _FLAGS_CHANGED:
                return
            pressed = bool(flags & self._flag)
        else:  # normal key → down/up events
            if etype == _KEY_DOWN:
                if repeat:
                    return
                pressed = True
            elif etype == _KEY_UP:
                pressed = False
            else:
                return
        if pressed and not self._down:
            self._down = True
            self._on_press()
        elif not pressed and self._down:
            self._down = False
            self._on_release()

    def start(self) -> None:
        import Quartz

        mask = (
            Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
            | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
        )

        def _cb(_proxy, etype, event, _refcon):
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            repeat = bool(
                Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
            )
            self._handle(int(etype), int(keycode), int(Quartz.CGEventGetFlags(event)), repeat)
            return event

        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            mask,
            _cb,
            None,
        )
        if self._tap is None:
            raise RuntimeError(_PERMISSION_HINT)
        self._thread = threading.Thread(target=self._run, name="neovis-hotkey", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import Quartz

        self._loop = Quartz.CFRunLoopGetCurrent()
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        Quartz.CFRunLoopRun()

    def stop(self) -> None:
        import Quartz

        if self._tap is not None:
            Quartz.CGEventTapEnable(self._tap, False)
        if self._loop is not None:
            Quartz.CFRunLoopStop(self._loop)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._tap = self._loop = self._thread = None


class _PynputHotkey:
    """pynput-backed listener for Windows/Linux."""

    def __init__(self, key_name: str, on_press, on_release):
        self._key_name = key_name
        self._on_press = on_press
        self._on_release = on_release
        self._listener = None
        self._down = False

    def start(self) -> None:
        from pynput import keyboard

        key = getattr(keyboard.Key, self._key_name, None)
        if key is None:
            raise ValueError(f"unknown hotkey {self._key_name!r} (use e.g. ctrl_r, alt_r, f5)")

        def on_press(k):
            if k == key and not self._down:
                self._down = True
                self._on_press()

        def on_release(k):
            if k == key and self._down:
                self._down = False
                self._on_release()

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


def HotkeyListener(key_name: str, on_press, on_release):  # noqa: N802 — factory
    if sys.platform == "darwin":
        return _DarwinHotkey(key_name, on_press, on_release)
    return _PynputHotkey(key_name, on_press, on_release)
