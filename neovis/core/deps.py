"""Per-run dependencies handed to every tool via ``RunContext``.

Bundling policy, audit, approval and the cancel flag here means individual tool
authors never touch enforcement — the registry wrapper does, using these deps.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .approval import ApprovalGateway
from .audit import AuditLog
from .policy import PolicyConfig


class CancelToken:
    """Cooperative cancellation. Slack 'stop' / Ctrl-C sets it; tools check it."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass
class NeovisDeps:
    policy: PolicyConfig
    audit: AuditLog
    approval: ApprovalGateway
    session_id: str = "default"
    actor: str = "cli"                     # who is driving (slack user id / "cli")
    cancel: CancelToken = field(default_factory=CancelToken)
