"""Siri-style floating capsule for the desktop voice flow.

A frameless, always-on-top pill at the top of the screen (pattern borrowed from
ThunderTalk's VoiceOverlay, re-themed for Neovis):

  listening   → green waveform bars driven by real mic level
  thinking    → shimmer text (your transcript, then each agent step as it runs)
  response    → the short spoken line; if there's more detail, the pill expands
                into a Siri-like panel with the full text (click to dismiss)
  error       → red tint, short message

All state changes arrive via OverlayBridge signals so the daemon thread never
touches widgets; GuiVoiceUI is the thread-safe object handed to the voice loop.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

_PILL_W, _PILL_H = 400, 58
_PANEL_W, _PANEL_H_MAX = 480, 460

_GREEN = QColor("#34C759")
_TEXT = QColor("#F2F2F4")
_MUTED = QColor("#A8ACB6")
_ERR = QColor("#FF6B63")


class OverlayBridge(QObject):
    """Daemon-thread → GUI-thread marshalling for the overlay."""

    listening = Signal()
    level = Signal(float)
    thinking = Signal(str)
    step = Signal(str)
    response = Signal(str, str)  # (spoken, detail)
    error = Signal(str)
    idle = Signal()


class GuiVoiceUI:
    """The VoiceUI implementation handed to the voice loop (duck-typed).
    Safe to call from any thread — every method just emits a queued signal."""

    def __init__(self, bridge: OverlayBridge):
        self._b = bridge

    def listening(self) -> None:
        self._b.listening.emit()

    def level(self, rms: float) -> None:
        self._b.level.emit(float(rms))

    def thinking(self, text: str) -> None:
        self._b.thinking.emit(text or "")

    def step(self, line: str) -> None:
        self._b.step.emit(line or "")

    def response(self, spoken: str, detail: str) -> None:
        self._b.response.emit(spoken or "", detail or "")

    def error(self, msg: str) -> None:
        self._b.error.emit(msg or "")

    def idle(self) -> None:
        self._b.idle.emit()


class NeovisOverlay(QWidget):
    _IDLE, _LISTEN, _THINK, _RESPONSE, _ERROR = range(5)

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.PointingHandCursor)

        self._state = self._IDLE
        self._text = ""          # pill line (transcript / step / spoken)
        self._headline = ""      # response panel headline
        self._detail = ""        # response panel body
        self._phase = 0.0
        self._rms = 0.0
        self._smooth = 0.0
        self._hide_token = 0     # invalidates stale auto-hide timers
        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)

    # ── slots (connect OverlayBridge here) ───────────────────────────────────
    def show_listening(self) -> None:
        self._enter(self._LISTEN, "Listening…", pill=True)
        self._anim.start(16)

    def set_level(self, rms: float) -> None:
        self._rms = rms

    def show_thinking(self, line: str) -> None:
        self._enter(self._THINK, line or "Thinking…", pill=True)
        if not self._anim.isActive():
            self._anim.start(16)

    def set_step(self, line: str) -> None:
        if self._state == self._THINK and line:
            self._text = line
            self.update()

    def show_response(self, spoken: str, detail: str) -> None:
        self._headline, self._detail = spoken, detail
        if detail:  # size the panel to its content
            self._head_h, self._panel_h = self._measure_panel(spoken, detail)
        self._enter(self._RESPONSE, spoken, pill=not detail)
        self._anim.stop()
        self._auto_hide(16000 if detail else 6000)

    def show_error(self, msg: str) -> None:
        self._enter(self._ERROR, msg[:60], pill=True)
        self._anim.stop()
        self._auto_hide(3000)

    def hide_overlay(self) -> None:
        self._hide_token += 1
        self._anim.stop()
        self._state = self._IDLE
        self.hide()

    # ── internals ────────────────────────────────────────────────────────────
    def _measure_panel(self, spoken: str, detail: str) -> tuple[int, int]:
        """(headline height, total panel height) fitted to the text."""
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QFontMetrics

        avail = _PANEL_W - 48
        fh = QFont()
        fh.setPixelSize(15)
        fh.setWeight(QFont.Weight.DemiBold)
        head_h = QFontMetrics(fh).boundingRect(
            QRect(0, 0, avail, 400), Qt.TextFlag.TextWordWrap, spoken).height()
        fb = QFont()
        fb.setPixelSize(13)
        body_h = QFontMetrics(fb).boundingRect(
            QRect(0, 0, avail, 4000), Qt.TextFlag.TextWordWrap, detail).height()
        total = 18 + head_h + 14 + 1 + 12 + body_h + 18
        return head_h, max(140, min(int(_PANEL_H_MAX), int(total)))

    def _enter(self, state: int, text: str, *, pill: bool) -> None:
        self._hide_token += 1
        self._state = state
        self._text = text
        w, h = (_PILL_W, _PILL_H) if pill else (_PANEL_W, getattr(self, "_panel_h", 300))
        self.setFixedSize(w, h)
        s = self.screen()
        if s:
            g = s.availableGeometry()
            self.move(g.x() + (g.width() - w) // 2, g.y() + 72)
        self.show()
        self.update()

    def _auto_hide(self, ms: int) -> None:
        token = self._hide_token

        def maybe_hide():
            if token == self._hide_token:
                self.hide_overlay()

        QTimer.singleShot(ms, maybe_hide)

    def mousePressEvent(self, _e) -> None:  # noqa: N802 — click to dismiss
        self.hide_overlay()

    def _tick(self) -> None:
        self._phase += 0.08
        target = min(1.0, self._rms * 10.0)
        k = 0.24 if target > self._smooth else 0.07
        self._smooth += (target - self._smooth) * k
        self.update()

    # ── paint ────────────────────────────────────────────────────────────────
    def paintEvent(self, _e) -> None:  # noqa: N802
        if self._state == self._IDLE:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        radius = _PILL_H / 2 if h <= _PILL_H else 22

        shape = QPainterPath()
        shape.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        bg = QColor(40, 16, 16, 242) if self._state == self._ERROR else QColor(19, 20, 24, 242)
        p.fillPath(shape, bg)
        p.setPen(QPen(QColor(255, 255, 255, 16), 1))
        p.drawPath(shape)

        if self._state == self._LISTEN:
            self._paint_listening(p, w, h)
        elif self._state == self._THINK:
            self._paint_thinking(p, w, h)
        elif self._state == self._RESPONSE and self._detail:
            self._paint_panel(p, w, h)
        else:
            self._paint_line(p, w, h)
        p.end()

    def _paint_listening(self, p: QPainter, w: int, h: int) -> None:
        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(_TEXT)
        p.drawText(24, int(h / 2 + 5), self._text)

        lv = self._smooth
        bx, bw, nb = 150, w - 150 - 26, 30
        sp = bw / nb
        for i in range(nb):
            x = bx + i * sp
            wave = 0.5 + 0.5 * math.sin(self._phase * 2.0 + i * 0.32)
            amp = 0.05 + 0.95 * lv * wave
            bh = max(2, int(22 * amp))
            y = int(h / 2 - bh / 2)
            c = QColor(_GREEN)
            c.setAlpha(int(140 + 110 * amp))
            p.setPen(QPen(c, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(int(x), y, int(x), y + bh)

    def _paint_thinking(self, p: QPainter, w: int, h: int) -> None:
        breath = 0.5 + 0.5 * math.sin(self._phase * 1.4)
        glow = QRadialGradient(QPointF(w / 2, h / 2), w * 0.55)
        glow.setColorAt(0.0, QColor(52, 199, 89, int(46 + 40 * breath)))
        glow.setColorAt(0.5, QColor(40, 150, 70, int(15 + 12 * breath)))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)

        f = QFont()
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        fm = p.fontMetrics()
        text = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, w - 48)
        tw = fm.horizontalAdvance(text)
        tx = int((w - tw) / 2)
        ty = int(h / 2 + fm.ascent() / 2 - 2)

        cyc = (self._phase * 0.04) % 1.0
        sweep = (tw + 120) * cyc - 60
        g = QLinearGradient(tx + sweep - 60, 0, tx + sweep + 60, 0)
        for stop, col in ((0.0, QColor(175, 178, 186)), (0.45, QColor(175, 178, 186)),
                          (0.5, QColor(255, 255, 255)), (0.55, QColor(175, 178, 186)),
                          (1.0, QColor(175, 178, 186))):
            g.setColorAt(stop, col)
        p.setPen(QPen(QBrush(g), 1))
        p.drawText(tx, ty, text)

    def _paint_line(self, p: QPainter, w: int, h: int) -> None:
        f = QFont()
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(_ERR if self._state == self._ERROR else _TEXT)
        text = p.fontMetrics().elidedText(self._text, Qt.TextElideMode.ElideRight, w - 48)
        p.drawText(QRectF(24, 0, w - 48, h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

    def _paint_panel(self, p: QPainter, w: int, h: int) -> None:
        pad = 24
        head_h = getattr(self, "_head_h", 24)
        f = QFont()
        f.setPixelSize(15)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(_TEXT)
        p.drawText(QRectF(pad, 18, w - 2 * pad, head_h + 4),
                   Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, self._headline)

        div_y = 18 + head_h + 14
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(pad, div_y, w - pad, div_y)

        f2 = QFont()
        f2.setPixelSize(13)
        p.setFont(f2)
        p.setPen(_MUTED)
        body = QRectF(pad, div_y + 12, w - 2 * pad, h - (div_y + 12) - 14)
        p.drawText(body, Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap, self._detail)
