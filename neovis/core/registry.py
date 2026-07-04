"""Tool registry — the single chokepoint where enforcement happens.

Tool authors write a plain, typed Python function and decorate it with
``@tool(risk=...)``. They never touch approval, audit, denylists or schemas.
The registry:

* infers the JSON schema from type hints,
* wraps the function so that on every call it runs (in order):
  precheck (hard deny) → approval (if DANGEROUS) → execute → audit.

Because enforcement lives here and not in the tools, there is no code path that
reaches a dangerous action without passing the gate.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import typing
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import TypeAdapter
from pydantic_ai import RunContext, Tool

from .approval import ApprovalRequest
from .audit import AuditRecord
from .deps import NeovisDeps
from .policy import PolicyConfig
from .risk import Risk

# A precheck inspects the (policy, kwargs) and returns a deny-reason or None.
Precheck = Callable[[PolicyConfig, dict[str, Any]], "str | None"]

_PLATFORM = {"darwin": "darwin", "linux": "linux", "win32": "windows"}.get(
    sys.platform, sys.platform
)


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    risk: Risk
    platforms: tuple[str, ...] = ()      # () => all platforms
    precheck: Precheck | None = None
    is_async: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._specs[spec.name] = spec

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def get(self, name: str) -> ToolSpec:
        return self._specs[name]

    def build_tools(self) -> list[Tool]:
        """Materialise pydantic-ai Tools available on the current platform."""
        return [
            _build_enforced_tool(s)
            for s in self._specs.values()
            if not s.platforms or _PLATFORM in s.platforms
        ]


# Module-level default registry; tool modules populate it at import time.
REGISTRY = ToolRegistry()


def tool(
    *,
    risk: str | int | Risk,
    name: str | None = None,
    description: str | None = None,
    platforms: tuple[str, ...] = (),
    precheck: Precheck | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``fn`` as an agent tool. See module docstring for the contract."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=name or fn.__name__,
            description=(description or inspect.getdoc(fn) or "").strip(),
            fn=fn,
            risk=Risk.parse(risk),
            platforms=tuple(platforms),
            precheck=precheck,
            is_async=inspect.iscoroutinefunction(fn),
        )
        (registry or REGISTRY).register(spec)
        return fn

    return decorator


def schema_from_function(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build a strict JSON object schema from a function's type hints."""
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("ctx", "self"):
            continue
        annotation = hints.get(pname, str)
        props[pname] = TypeAdapter(annotation).json_schema()
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _summarise(spec: ToolSpec, kwargs: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v!r}" for k, v in list(kwargs.items())[:4])
    return f"{spec.name}({shown})"


def _build_enforced_tool(spec: ToolSpec) -> Tool:
    schema = schema_from_function(spec.fn)

    async def run(ctx: RunContext[NeovisDeps], **kwargs: Any) -> Any:
        deps = ctx.deps
        eff = deps.policy.effective_risk(spec.name, spec.risk)

        if deps.cancel.is_cancelled():
            return "CANCELLED: a stop was requested; not running further actions."

        def audit(status: str, result: str | None, approver: str | None = None) -> None:
            deps.audit.record(
                AuditRecord(
                    tool=spec.name,
                    risk=eff.name,
                    status=status,
                    args=kwargs,
                    session_id=deps.session_id,
                    actor=deps.actor,
                    approver=approver,
                    result=result,
                )
            )

        # 1) Hard deny (denylist / sandbox) — cannot be approved away.
        if spec.precheck is not None:
            reason = spec.precheck(deps.policy, kwargs)
            if reason:
                audit("denied", reason)
                return f"DENIED by policy: {reason}"

        # 2) Approval for dangerous actions.
        approver: str | None = None
        if eff >= Risk.DANGEROUS:
            if deps.policy.auto_approve_dangerous:
                approver = "auto"
            else:
                decision = await deps.approval.request(
                    ApprovalRequest(
                        tool=spec.name,
                        args=kwargs,
                        risk=eff.name,
                        session_id=deps.session_id,
                        summary=_summarise(spec, kwargs),
                    )
                )
                if not decision.approved:
                    audit("rejected", decision.reason)
                    return f"REJECTED: {decision.reason or 'not approved by human'}"
                approver = decision.approver

        # 3) Execute (offload sync tools to a thread so the loop stays responsive).
        try:
            if spec.is_async:
                result = await spec.fn(**kwargs)
            else:
                result = await asyncio.to_thread(spec.fn, **kwargs)
        except Exception as exc:  # surface as text so the agent can react/retry
            audit("error", repr(exc), approver)
            return f"ERROR running {spec.name}: {exc}"

        audit("ok", result if isinstance(result, str) else str(result), approver)
        return result

    return Tool.from_schema(
        function=run,
        name=spec.name,
        description=spec.description,
        json_schema=schema,
        takes_ctx=True,
    )
