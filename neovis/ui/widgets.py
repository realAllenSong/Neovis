"""Animated widgets for the Neovis window.

Pure QPainter + Qt's animation framework (QVariantAnimation / QPropertyAnimation
with easing curves) — the native-app equivalent of the web's GSAP/Lottie: no
extra dependencies, and pixel-identical on macOS, Windows, and Linux (emoji
glyphs and web views are not).
"""

from __future__ import annotations

import math

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

STATE_COLORS = {
    "on": QColor("#34C759"),        # live
    "starting": QColor("#FFC53D"),  # warming up (breathes)
    "error": QColor("#FF5D55"),     # failed — message shown next to it
    "off": QColor("#4A4D55"),       # idle
}


class _Dot(QWidget):
    """A painted status dot: grey when off, amber and breathing while starting,
    green with a soft glow when live, red on error."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._state = "off"
        self._phase = 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(1400)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._tick)

    def _tick(self, v) -> None:
        self._phase = float(v)
        self.update()

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        if state == "starting":
            self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(STATE_COLORS.get(self._state, STATE_COLORS["off"]))
        center = QPointF(self.width() / 2, self.height() / 2)
        if self._state == "starting":
            # breathe: the dot swells while a faint ring expands and fades
            pulse = 0.5 - 0.5 * math.cos(2 * math.pi * self._phase)
            ring = QColor(c)
            ring.setAlphaF(0.5 * (1.0 - pulse))
            p.setPen(QPen(ring, 1.5))
            p.setBrush(Qt.NoBrush)
            r = 3.5 + 4.0 * pulse
            p.drawEllipse(center, r, r)
            c.setAlphaF(0.45 + 0.55 * pulse)
        elif self._state == "on":
            glow = QColor(c)
            glow.setAlphaF(0.22)
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(center, 7.5, 7.5)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(center, 4.5, 4.5)


class ChannelStatus(QWidget):
    """Dot + channel name + a short status message ('starting…', an error)."""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self._dot = _Dot()
        self._name = QLabel(name)
        self._msg = QLabel("")
        self._msg.setObjectName("dotmsg")
        row.addWidget(self._dot)
        row.addWidget(self._name)
        row.addWidget(self._msg)

    def set_state(self, state: str, msg: str = "") -> None:
        self._dot.set_state(state)
        self._msg.setText(f"· {str(msg)[:38]}" if msg else "")


class PrimaryButton(QPushButton):
    """The Start/Stop button. Green ↔ red with an eased color cross-fade;
    hover brightens, press dims — all custom-painted."""

    _GREEN, _GREEN_H = QColor("#2FA84F"), QColor("#39BF5C")
    _RED, _RED_H = QColor("#C24840"), QColor("#D2554C")

    def __init__(self, text: str = "Start", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(52)
        self._running = False
        self._hover = False
        self._bg = QColor(self._GREEN)
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._on_color)
        f = self.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() + 2)
        self.setFont(f)

    def _on_color(self, v) -> None:
        self._bg = QColor(v)
        self.update()

    def _target(self) -> QColor:
        if self._running:
            return self._RED_H if self._hover else self._RED
        return self._GREEN_H if self._hover else self._GREEN

    def _animate_to_target(self, duration: int) -> None:
        self._anim.stop()
        self._anim.setDuration(duration)
        self._anim.setStartValue(QColor(self._bg))
        self._anim.setEndValue(self._target())
        self._anim.start()

    def set_running(self, running: bool) -> None:
        if running == self._running:
            return
        self._running = running
        self.setText("Stop" if running else "Start")
        self._animate_to_target(320)

    def enterEvent(self, e) -> None:  # noqa: N802
        self._hover = True
        self._animate_to_target(140)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:  # noqa: N802
        self._hover = False
        self._animate_to_target(180)
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self._bg)
        if self.isDown():
            c = c.darker(114)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(self.rect(), 12, 12)
        p.setPen(QColor("white"))
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignCenter, self.text())


class Collapsible(QWidget):
    """Wraps a widget and slides it open/closed by animating maximumHeight.
    Emits `resized` every animation frame so the window can follow."""

    resized = Signal()

    def __init__(self, content: QWidget, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(content)
        self._content = content
        self._open = True
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._anim.valueChanged.connect(lambda *_: self.resized.emit())
        self._anim.finished.connect(self._finished)

    def set_open(self, open_: bool, animate: bool = True) -> None:
        self._open = open_
        target = self._content.sizeHint().height() if open_ else 0
        if not animate:
            self.setMaximumHeight(16_777_215 if open_ else 0)
            self.resized.emit()
            return
        self._anim.stop()
        self._anim.setStartValue(self.height())
        self._anim.setEndValue(target)
        self._anim.start()

    def _finished(self) -> None:
        if self._open:
            self.setMaximumHeight(16_777_215)  # let it grow naturally again
        self.resized.emit()
