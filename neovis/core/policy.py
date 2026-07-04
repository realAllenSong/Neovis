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
