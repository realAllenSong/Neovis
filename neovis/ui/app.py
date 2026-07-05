"""Neovis — a small, elegant control window.

Click Start; Neovis is then reachable two ways and nothing else:
  • DM the Neovis bot in Slack (from your phone), and
  • hold your push-to-talk key anywhere on the machine and speak.

No terminal, no commands. The push-to-talk key and voice are set right here.

    neovis-app     (or: python -m neovis.ui.app)
"""

from __future__ import annotations

import concurrent.futures
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..core.approval import ApprovalDecision, ApprovalGateway, ApprovalRequest
from ..core.settings import load as load_settings
from ..core.settings import save as save_settings
from .daemon import NeovisDaemon
from .overlay import GuiVoiceUI, NeovisOverlay, OverlayBridge
from .widgets import ChannelStatus, Collapsible, PrimaryButton

# Friendly hotkey labels, per platform (pynput key names underneath).
if sys.platform == "darwin":
    _HOTKEYS = [("Right ⌘", "cmd_r"), ("Left ⌘", "cmd_l"), ("Right ⌥", "alt_r"),
                ("Right ⌃", "ctrl_r"), ("F5", "f5")]
elif sys.platform.startswith("win"):
    _HOTKEYS = [("Right Ctrl", "ctrl_r"), ("Right Alt", "alt_r"),
                ("Right Shift", "shift_r"), ("F5", "f5")]
else:  # Linux
    _HOTKEYS = [("Right Ctrl", "ctrl_r"), ("Right Alt", "alt_r"),
                ("Right Shift", "shift_r"), ("Super", "cmd"), ("F5", "f5")]

_VOICES = [("Sky · US ♀", "sky"), ("Adam · US ♂", "adam"),
           ("Emma · UK ♀", "emma"), ("George · UK ♂", "george")]

_QSS = """
* { color: #E8E8EA; }
QWidget#root { background: #16171B; }
QLabel#title { font-size: 26px; font-weight: 600; }
QLabel#subtitle { color: #8A8D96; font-size: 13px; }
QLabel#section { color: #8A8D96; font-size: 11px; letter-spacing: 1px; }
QLabel#hint { color: #6E727C; font-size: 12px; }
QLabel#dotmsg { color: #9AA0AA; font-size: 12px; }
QFrame#divider { background: #2A2C33; max-height: 1px; }
QComboBox, QLineEdit { background: #23252B; border: 1px solid #33363E; border-radius: 8px;
    padding: 7px 10px; font-size: 13px; }
QComboBox:hover, QLineEdit:focus { border-color: #4C6EF5; }
QComboBox:disabled, QLineEdit:disabled { color: #55585F; background: #1B1C21; border-color: #26282E; }
QCheckBox { font-size: 13px; }
QCheckBox:disabled, QLabel:disabled { color: #55585F; }
QToolTip { background: #23252B; color: #E8E8EA; border: 1px solid #33363E; padding: 4px; }
"""


class _ApprovalBridge(QObject):
    """Marshals an approval request from the daemon thread to the GUI thread."""

    request = Signal(object, object)  # (ApprovalRequest, concurrent.futures.Future)


class GuiApproval(ApprovalGateway):
    """Voice actions that write or reach outward pop a small Allow/Deny dialog."""

    def __init__(self, bridge: _ApprovalBridge):
        self._bridge = bridge

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        import asyncio

        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._bridge.request.emit(req, fut)
        return await asyncio.get_event_loop().run_in_executor(None, fut.result)


class NeovisWindow(QWidget):
    _status_changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self._bridge = _ApprovalBridge()
        self._bridge.request.connect(self._on_approval_request)
        # Siri-style capsule: daemon-thread voice events → queued signals → overlay
        self.overlay = NeovisOverlay()
        self._voice_bridge = OverlayBridge()
        b, o = self._voice_bridge, self.overlay
        b.listening.connect(o.show_listening)
        b.level.connect(o.set_level)
        b.thinking.connect(o.show_thinking)
        b.step.connect(o.set_step)
        b.response.connect(o.show_response)
        b.error.connect(o.show_error)
        b.idle.connect(o.hide_overlay)
        self.daemon = NeovisDaemon(
            on_status=lambda s: self._status_changed.emit(s),
            approval_factory=lambda: GuiApproval(self._bridge),
            voice_ui_factory=lambda: GuiVoiceUI(self._voice_bridge),
        )
        self._status_changed.connect(self._render_status)
        self._build()

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setObjectName("root")
        self.setWindowTitle("Neovis")
        self.setFixedWidth(420)
        self.setStyleSheet(_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        root.setSpacing(16)

        title = QLabel("Neovis")
        title.setObjectName("title")
        root.addWidget(title)
        sub = QLabel("Your workstation, on call.")
        sub.setObjectName("subtitle")
        root.addWidget(sub)

        # live channel status
        self.st_slack = ChannelStatus("Slack")
        self.st_voice = ChannelStatus("Voice")
        status_row = QHBoxLayout()
        status_row.setSpacing(18)
        status_row.addWidget(self.st_slack)
        status_row.addWidget(self.st_voice)
        status_row.addStretch()
        root.addSpacing(2)
        root.addLayout(status_row)

        # the one button
        self.btn = PrimaryButton("Start")
        self.btn.setToolTip("Bring Neovis up (Slack + voice). Click again to stop everything.")
        self.btn.clicked.connect(self._toggle)
        root.addSpacing(4)
        root.addWidget(self.btn)

        # settings (locked while running — stop to change)
        self.box_settings = QWidget()
        box = QVBoxLayout(self.box_settings)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(16)

        box.addWidget(self._divider())
        box.addWidget(self._label("SETTINGS", "section"))

        self.chk_voice = QCheckBox("Enable voice (hold a key to talk)")
        self.chk_voice.setChecked(bool(self.settings.get("voice_enabled", True)))
        self.chk_voice.setToolTip("Run the local voice loop (microphone + speech).\nTurn off to use Slack only.")
        self.chk_voice.stateChanged.connect(self._on_voice_toggle)
        box.addWidget(self.chk_voice)

        voice_inner = QWidget()
        vbox = QVBoxLayout(voice_inner)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(16)
        self.cb_key = self._combo(_HOTKEYS, self.settings.get("hotkey", _HOTKEYS[0][1]))
        self.cb_key.setToolTip("The key you hold — anywhere, in any app — to talk to Neovis.")
        vbox.addLayout(self._field("Push-to-talk key", self.cb_key))
        self.cb_voice = self._combo(_VOICES, self.settings.get("voice", "sky"))
        self.cb_voice.setToolTip("Neovis's speaking voice. You can also just ask it —\n“make it a British male voice”.")
        vbox.addLayout(self._field("Voice", self.cb_voice))
        self.chk_hf = QCheckBox("Hands-free (talk anytime, no key)")
        self.chk_hf.setChecked(bool(self.settings.get("hands_free")))
        self.chk_hf.setToolTip("No key needed — Neovis listens continuously and answers when\nyou pause. The mic mutes while Neovis speaks (no echo).")
        self.chk_hf.stateChanged.connect(self._save)
        vbox.addWidget(self.chk_hf)
        self.chk_bi = QCheckBox("Barge-in (interrupt it by talking — headphones!)")
        self.chk_bi.setChecked(bool(self.settings.get("barge_in")))
        self.chk_bi.setToolTip("Keep listening WHILE Neovis speaks so you can cut it off.\nOnly with headphones — on open speakers it hears itself.")
        self.chk_bi.stateChanged.connect(self._save)
        vbox.addWidget(self.chk_bi)

        self.fold = Collapsible(voice_inner)
        self.fold.resized.connect(self.adjustSize)
        box.addWidget(self.fold)

        box.addWidget(self._divider())
        box.addWidget(self._label("SLACK (optional — control from your phone)", "section"))
        self.ed_bot = self._secret("Bot token  xoxb-…", self.settings.get("slack_bot_token", ""))
        self.ed_bot.setToolTip("api.slack.com → your app → OAuth & Permissions → Bot User OAuth Token")
        self.ed_app = self._secret("App token  xapp-…", self.settings.get("slack_app_token", ""))
        self.ed_app.setToolTip("api.slack.com → your app → Basic Information → App-Level Tokens (connections:write)")
        box.addWidget(self.ed_bot)
        box.addWidget(self.ed_app)
        root.addWidget(self.box_settings)

        # dynamic hint — tells you exactly what to do in the current state
        self.hint = QLabel("")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        root.addSpacing(4)
        root.addWidget(self.hint)

        for w in (self.cb_key, self.cb_voice):
            w.currentIndexChanged.connect(self._save)
        for w in (self.ed_bot, self.ed_app):
            w.editingFinished.connect(self._save)

        self.fold.set_open(self.chk_voice.isChecked(), animate=False)
        self._render_status(self.daemon.status)

    def _divider(self) -> QFrame:
        f = QFrame()
        f.setObjectName("divider")
        f.setFrameShape(QFrame.HLine)
        return f

    def _label(self, text: str, obj: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(obj)
        return lbl

    def _combo(self, pairs, current) -> QComboBox:
        box = QComboBox()
        for label, value in pairs:
            box.addItem(label, value)
        values = [v for _, v in pairs]
        box.setCurrentIndex(values.index(current) if current in values else 0)
        return box

    def _field(self, label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lab = QLabel(label)
        lab.setFixedWidth(150)
        row.addWidget(lab)
        row.addWidget(widget, 1)
        return row

    def _secret(self, placeholder: str, value: str) -> QLineEdit:
        ed = QLineEdit()
        ed.setPlaceholderText(placeholder)
        ed.setText(value)
        ed.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        return ed

    # ── behaviour ────────────────────────────────────────────────────────────
    def _current_settings(self) -> dict:
        return {
            "voice_enabled": self.chk_voice.isChecked(),
            "hotkey": self.cb_key.currentData(),
            "voice": self.cb_voice.currentData(),
            "hands_free": self.chk_hf.isChecked(),
            "barge_in": self.chk_bi.isChecked(),
            "hotwords": self.settings.get("hotwords", []),
            "slack_bot_token": self.ed_bot.text().strip(),
            "slack_app_token": self.ed_app.text().strip(),
        }

    def _on_voice_toggle(self, *_) -> None:
        self.fold.set_open(self.chk_voice.isChecked())
        self._save()

    def _save(self, *_) -> None:
        self.settings = self._current_settings()
        save_settings(self.settings)
        self._render_status(self.daemon.status)  # keep the hint line in sync

    def _toggle(self) -> None:
        if self.daemon.is_running():
            self.daemon.stop()
        else:
            self._save()
            self.daemon.start(self.settings)

    @staticmethod
    def _norm(state) -> str:
        if isinstance(state, bool):  # tolerate the old bool shape
            return "on" if state else "off"
        return state if state in ("on", "starting", "error", "off") else "off"

    def _render_status(self, status: dict) -> None:
        running = bool(status.get("running"))
        slack = self._norm(status.get("slack"))
        voice = self._norm(status.get("voice"))
        self.btn.set_running(running)
        self.box_settings.setEnabled(not running)  # stop to change settings
        self.st_slack.set_state(slack, status.get("slack_msg", ""))
        self.st_voice.set_state(voice, status.get("voice_msg", ""))
        self.hint.setText(self._hint_text(running, slack, voice))

    def _hint_text(self, running: bool, slack: str, voice: str) -> str:
        key = self.cb_key.currentText()
        if not running:
            ways = []
            if self.ed_bot.text().strip():
                ways.append("DM the Neovis bot in Slack")
            if self.chk_voice.isChecked():
                ways.append(f"hold {key} anywhere and talk")
            return "Click Start — then " + " · or ".join(ways or ["add your Slack tokens below"]) + "."
        ways = []
        if slack == "on":
            ways.append("DM the Neovis bot in Slack")
        if voice == "on":
            ways.append(f"hold {key} anywhere and talk")
        if ways:
            return "Ready — " + " · or ".join(ways) + ".  (Stop to change settings.)"
        if "starting" in (slack, voice):
            return "Warming up…"
        return "Nothing is running — check the status above, then Stop and try again."

    def _on_approval_request(self, req: ApprovalRequest, fut: concurrent.futures.Future) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Neovis needs your OK")
        severe = " (irreversible)" if req.severe else ""
        box.setText(f"Allow this{severe}?\n\n{req.summary or req.tool}")
        allow = box.addButton("Allow", QMessageBox.AcceptRole)
        auto = box.addButton("Allow all (this task)", QMessageBox.YesRole)
        box.addButton("Deny", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is allow:
            fut.set_result(ApprovalDecision(True, approver="gui", scope="once"))
        elif clicked is auto:
            fut.set_result(ApprovalDecision(True, approver="gui", scope="auto"))
        else:
            fut.set_result(ApprovalDecision(False, reason="denied in the app"))

    def closeEvent(self, event) -> None:  # noqa: N802
        self.daemon.stop()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    win = NeovisWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
