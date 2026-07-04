"""Reusable prechecks: hard denials evaluated before approval or execution."""

from __future__ import annotations

from typing import Any, Callable

from ..core.policy import PolicyConfig


def shell_precheck(policy: PolicyConfig, kwargs: dict[str, Any]) -> str | None:
    command = str(kwargs.get("command", ""))
    rule = policy.is_shell_denied(command)
    if rule:
        return f"command matches denylist rule /{rule}/"
    return None


def sandbox_precheck(key: str) -> Callable[[PolicyConfig, dict[str, Any]], "str | None"]:
    """Deny if the path in ``kwargs[key]`` escapes the configured sandbox roots."""

    def _check(policy: PolicyConfig, kwargs: dict[str, Any]) -> str | None:
        path = kwargs.get(key)
        if path and not policy.is_path_allowed(str(path)):
            return f"{key}={path!r} is outside the allowed sandbox roots"
        return None

    return _check
