"""The live agent session — the Claude Agent SDK engine wired to Neovis's gate.

`ClaudeAgentOptions.can_use_tool` receives our consequence-gated callback
(:mod:`.gate`), so every tool the model wants to run is classified and, if it
writes or reaches outward, routed to a human via an approval gateway. `env`
points the bundled engine at the model endpoint — the company Claude proxy, or a
LiteLLM gateway fronting any OpenAI-compatible model (GPT/GLM/…). We set
``setting_sources=[]`` so the gate is the *only* authority — no local Claude Code
settings can silently pre-approve a tool.
"""

from __future__ import annotations

import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .approval import ApprovalGateway
from .audit import AuditLog
from .config import AppConfig
from .gate import AutoMode, build_can_use_tool

SYSTEM_PROMPT = """\
You are Neovis, a JARVIS-style operator running on a colleague's work computer at
a hedge fund. You carry out requests by operating this machine and its apps —
the shell, the filesystem, the browser. Principles:
- Be decisive: prefer doing the task with tools over describing it.
- Actions are gated by consequence. Reads run freely; writes and especially
  outward or irreversible actions (sending, submitting, deleting, pushing) pause
  for the human's approval. If a tool call comes back denied or rejected, STOP
  and explain — never try to route around the gate.
- Never fabricate results; report exactly what a tool returned.
- Keep replies short; the user is often on their phone.
"""

# Tools the model may use. The gate governs *whether* each call runs.
DEFAULT_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit", "Bash"]


def build_options(
    config: AppConfig,
    can_use_tool,
    *,
    gateway_url: str | None = None,
    gateway_key: str | None = None,
    allowed_tools: list[str] | None = None,
    mcp_servers: dict | None = None,
) -> ClaudeAgentOptions:
    """Assemble ClaudeAgentOptions pointing the engine at the model endpoint."""
    env = dict(os.environ)
    base = gateway_url or config.llm.base_url
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    key = gateway_key or os.environ.get(config.llm.api_key_env, "")
    if key:
        env["ANTHROPIC_API_KEY"] = key
        # Force api-key auth: if the host has a logged-in Claude Code, the engine
        # would otherwise present that OAuth credential to our gateway instead of
        # this key. Clear the OAuth path so ANTHROPIC_API_KEY wins.
        for stale in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop(stale, None)

    return ClaudeAgentOptions(
        model=config.llm.model,
        system_prompt=SYSTEM_PROMPT,
        can_use_tool=can_use_tool,
        permission_mode="default",       # defer to can_use_tool
        allowed_tools=allowed_tools or DEFAULT_ALLOWED_TOOLS,
        mcp_servers=mcp_servers or {},
        setting_sources=[],              # gate is the sole authority
        env=env,
        cwd=os.getcwd(),
    )


class NeovisSession:
    """A connected conversation whose tool use is consequence-gated."""

    def __init__(
        self,
        config: AppConfig,
        *,
        approval: ApprovalGateway,
        audit: AuditLog | None = None,
        actor: str = "user",
        session_id: str = "desktop",
        gateway_url: str | None = None,
        gateway_key: str | None = None,
        allowed_tools: list[str] | None = None,
        mcp_servers: dict | None = None,
    ):
        self.config = config
        self.audit = audit or AuditLog("neovis_audit.db")
        self.automode = AutoMode()
        self.approval = approval
        can_use_tool = build_can_use_tool(
            config.policy, self.audit, approval, self.automode,
            actor=actor, session_id=session_id,
        )
        self.options = build_options(
            config, can_use_tool,
            gateway_url=gateway_url, gateway_key=gateway_key,
            allowed_tools=allowed_tools, mcp_servers=mcp_servers,
        )
        self.client = ClaudeSDKClient(options=self.options)

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    async def send(self, message: str) -> str:
        """Run one top-level turn; auto-mode is scoped to this turn only."""
        self.automode.reset()
        await self.client.query(message)
        parts: list[str] = []
        async for msg in self.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                break
        return "".join(parts).strip()

    def stop(self) -> None:
        # cooperative interrupt of the in-flight task
        import asyncio

        asyncio.ensure_future(self.client.interrupt())
