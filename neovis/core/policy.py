"""Security policy: the guardrails that make Neovis safe to point at a work box.

Loaded from ``config/policy.yaml``. Two jobs:

1. Decide the *effective* risk of a tool (base tier + per-tool overrides).
2. Provide the concrete checks tools use to refuse unsafe arguments
   (shell command denylist, filesystem sandbox).
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from .risk import Risk


class PolicyConfig(BaseModel):
    """User-editable security posture."""

    # Substrings/regexes that, if present in a shell command, are always refused.
    shell_denylist: list[str] = Field(
        default_factory=lambda: [
            r"rm\s+-rf\s+/",
            r":\(\)\s*\{",          # fork bomb
            r"mkfs",
            r"dd\s+if=",
            r"\bshutdown\b",
            r"\breboot\b",
            r">\s*/dev/sd",
        ]
    )
    # Absolute path prefixes writes/deletes are confined to. Empty => home dir only.
    sandbox_roots: list[str] = Field(default_factory=list)
    # Tool name -> risk tier, to tighten or relax a specific tool without code changes.
    risk_overrides: dict[str, str] = Field(default_factory=dict)
    # Dev/demo convenience: auto-approve DANGEROUS tools instead of prompting.
    # Ships false; the Slack/console gateways are the real path.
    auto_approve_dangerous: bool = False

    # --- consequence-gating (Agent SDK permission model) ---------------------
    # Path prefixes / URL domains the agent may READ without asking.
    # Empty => reads are unrestricted (permissive demo default; a fund fills this).
    read_allowlist: list[str] = Field(default_factory=list)
    # Words that mark a write as outward-facing / irreversible → always confirm.
    outward_keywords: list[str] = Field(
        default_factory=lambda: [
            "send", "submit", "pay", "purchase", "confirm", "delete",
            "transfer", "wire", "publish", "checkout",
        ]
    )
    # Shell commands matching these regexes are treated as read-only.
    bash_read_patterns: list[str] = Field(
        default_factory=lambda: [
            r"^\s*ls(\s|$)", r"^\s*cat\s", r"^\s*head\s", r"^\s*tail\s",
            r"^\s*grep\s", r"^\s*rg\s", r"^\s*find\s", r"^\s*pwd(\s|$)",
            r"^\s*echo\s", r"^\s*which\s", r"^\s*git\s+(status|diff|log|show|branch)\b",
            r"^\s*ps(\s|$)", r"^\s*df(\s|$)", r"^\s*du\s", r"^\s*wc\s",
            r"^\s*stat\s", r"^\s*file\s", r"^\s*whoami\b", r"^\s*date\b",
        ]
    )
    # Shell commands matching these regexes are outward-facing / irreversible.
    bash_outward_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\bgit\s+push\b", r"\bcurl\b.*-X\s*(POST|PUT|DELETE|PATCH)",
            r"\bcurl\b.*\s-d\b", r"\bwget\b", r"\brm\s+-", r"\bssh\b",
            r"\bscp\b", r"\bmail\b", r"\bsendmail\b", r"\bgh\s+pr\s+create\b",
            r"\bnpm\s+publish\b",
        ]
    )

    def matches_any(self, patterns: list[str], text: str) -> bool:
        return any(re.search(p, text) for p in patterns)

    def read_allowed(self, target: str) -> bool:
        """True if a read target (path or URL) is inside the read allowlist."""
        if not self.read_allowlist:
            return True
        t = str(target)
        return any(entry in t for entry in self.read_allowlist)

    def effective_risk(self, tool_name: str, base: Risk) -> Risk:
        override = self.risk_overrides.get(tool_name)
        return Risk.parse(override) if override is not None else base

    def is_shell_denied(self, command: str) -> str | None:
        """Return the matching denylist rule if the command is forbidden, else None."""
        for rule in self.shell_denylist:
            if re.search(rule, command):
                return rule
        return None

    def _allowed_roots(self) -> list[Path]:
        if self.sandbox_roots:
            return [Path(r).expanduser().resolve() for r in self.sandbox_roots]
        return [Path.home().resolve()]

    def is_path_allowed(self, path: str | Path) -> bool:
        """True if ``path`` sits inside one of the sandbox roots."""
        target = Path(path).expanduser().resolve()
        for root in self._allowed_roots():
            if target == root or root in target.parents:
                return True
        return False
