"""Neovis — a small, elegant control window.

Click Start; Neovis is then reachable two ways and nothing else:
  • DM the Neovis bot in Slack (type or voice note), or
  • hold your push-to-talk key anywhere on the machine and speak.

No terminal, no commands. The push-to-talk key and voice are set right here.

    neovis-app     (or: python -m neovis.ui.app)
"""

from __future__ import annotations

import concurrent.futures
import sys

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.approval import ApprovalDecision, ApprovalGateway, ApprovalRequest
from ..core.settings import load as load_settings
from ..core.settings import save as save_settings
from ..voice.tts import VOICES
from .daemon import NeovisDaemon

# Friendly hotkey + voice labels
_HOTKEYS = [("Right ⌘", "cmd_r"), ("Left ⌘", "cmd_l"), ("Right ⌥", "alt_r"),
            ("Right ⌃", "ctrl_r"), ("F5", "f5")]
_VOICES = [("Sky · US ♀", "sky"), ("Adam · US ♂", "adam"),
           ("Emma · UK ♀", "emma"), ("George · UK ♂", "george")]

_QSS = """
* { font-family: -apple-system, 'SF Pro Text', 'Segoe UI', sans-serif; color: #E8E8EA; }
QWidget#root { background: #16171B; }
QLabel#title { font-size: 26px; font-weight: 600; }
QLabel#subtitle { color: #8A8D96; font-size: 13px; }
QLabel#section { color: #8A8D96; font-size: 11px; letter-spacing: 1px; }
QLabel#hint { color: #6E727C; font-size: 12px; }
QFrame#divider { background: #2A2C33; max-height: 1px; }
QComboBox, QLineEdit { background: #23252B; border: 1px solid #33363E; border-radius: 8px;
    padding: 7px 10px; font-size: 13px; }
QComboBox:hover, QLineEdit:focus { border-color: #4C6EF5; }
QCheckBox { font-size: 13px; }
QPushButton#primary { border: none; border-radius: 12px; padding: 14px; font-size: 15px;
    font-weight: 600; background: #3B8A4E; color: white; }
QPushButton#primary[running="true"] { background: #B5423A; }
QPushButton#primary:hover { background: #46A05C; }
QPushButton#primary[running="true"]:hover { background: #C64C43; }
QLabel#dot { font-size: 13px; }
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
        self.daemon = NeovisDaemon(
            on_status=lambda s: self._status_changed.emit(s),
            approval_factory=lambda: GuiApproval(self._bridge),
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

        # status dots
        self.dot_slack = QLabel()
        self.dot_voice = QLabel()
        status_row = QHBoxLayout()
        status_row.setSpacing(18)
        status_row.addWidget(self.dot_slack)
        status_row.addWidget(self.dot_voice)
        status_row.addStretch()
        root.addSpacing(4)
        root.addLayout(status_row)

        # primary button
        self.btn = QPushButton("Start")
        self.btn.setObjectName("primary")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._toggle)
        root.addSpacing(6)
        root.addWidget(self.btn)

        root.addWidget(self._divider())
        root.addWidget(self._label("SETTINGS", "section"))

        self.cb_key = self._combo(_HOTKEYS, self.settings.get("hotkey", "cmd_r"))
        root.addLayout(self._field("Push-to-talk key", self.cb_key))
        self.cb_voice = self._combo(_VOICES, self.settings.get("voice", "sky"))
        root.addLayout(self._field("Voice", self.cb_voice))

        self.chk_hf = QCheckBox("Hands-free (talk anytime, barge-in)")
        self.chk_hf.setChecked(bool(self.settings.get("hands_free")))
        self.chk_hf.stateChanged.connect(self._save)
        root.addWidget(self.chk_hf)

        root.addWidget(self._divider())
        root.addWidget(self._label("SLACK (optional — for phone control)", "section"))
        self.ed_bot = self._secret("Bot token  xoxb-…", self.settings.get("slack_bot_token", ""))
        self.ed_app = self._secret("App token  xapp-…", self.settings.get("slack_app_token", ""))
        root.addWidget(self.ed_bot)
        root.addWidget(self.ed_app)

        hint = QLabel("Hold your key anywhere and talk · or DM the Neovis bot in Slack.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        root.addSpacing(6)
        root.addWidget(hint)

        for w in (self.cb_key, self.cb_voice):
            w.currentIndexChanged.connect(self._save)
        for w in (self.ed_bot, self.ed_app):
            w.editingFinished.connect(self._save)

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
        idx = max(0, [v for _, v in pairs].index(current) if current in [v for _, v in pairs] else 0)
        box.setCurrentIndex(idx)
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
            "hotkey": self.cb_key.currentData(),
            "voice": self.cb_voice.currentData(),
            "hands_free": self.chk_hf.isChecked(),
            "hotwords": self.settings.get("hotwords", []),
            "slack_bot_token": self.ed_bot.text().strip(),
            "slack_app_token": self.ed_app.text().strip(),
        }

    def _save(self, *_) -> None:
        self.settings = self._current_settings()
        save_settings(self.settings)

    def _toggle(self) -> None:
        if self.daemon.is_running():
            self.daemon.stop()
        else:
            self._save()
            self.daemon.start(self.settings)

    def _render_status(self, status: dict) -> None:
        running = status.get("running")
        self.btn.setText("Stop" if running else "Start")
        self.btn.setProperty("running", "true" if running else "false")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.dot_slack.setText(("🟢" if status.get("slack") else "⚪") + "  Slack")
        self.dot_voice.setText(("🟢" if status.get("voice") else "⚪") + "  Voice")
        self.dot_slack.setObjectName("dot")
        self.dot_voice.setObjectName("dot")
        if status.get("error"):
            self.dot_voice.setText("🔴  " + status["error"][:40])

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
