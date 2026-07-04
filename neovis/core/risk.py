"""Risk tiers for tools.

Every tool declares a tier. The registry enforces it uniformly so there is no
way to run a dangerous action without passing through the approval gateway.
"""

from __future__ import annotations

from enum import IntEnum


class Risk(IntEnum):
    """Ordered so that stricter tiers compare greater."""

    SAFE = 0        # read-only: screenshot, list processes, read a file
    MODERATE = 1    # writes something local: create a file, open an app
    DANGEROUS = 2   # shell, delete, GUI control — requires approval

    @classmethod
    def parse(cls, value: str | int | "Risk") -> "Risk":
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(value)
        return cls[str(value).strip().upper()]
