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
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .approval import ApprovalGateway
from .audit import AuditLog
from .config import AppConfig
from .gate import AutoMode, PageContext, build_post_tool_use_hook, build_pre_tool_use_hook
from .memory import MEMORY_GUIDANCE, MemoryStore, build_memory_mcp
from .transcript import ConversationLog, build_recall_mcp

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

BROWSER_GUIDANCE = """

BROWSER MODE: a Chrome browser is ALREADY open and connected through the
chrome-devtools tools. Act in it ONLY with those tools — navigate_page,
take_snapshot, click, fill, fill_form, hover, take_screenshot. To open a site,
call navigate_page (never a shell command). NEVER launch, relaunch, restart, or
check Chrome, and never try to set up remote debugging — it is already done.
Take a snapshot before interacting so you have element uids. Work in small,
observable steps.
"""

def build_options(
    config: AppConfig,
    hooks: dict,
    *,
    gateway_url: str | None = None,
    gateway_key: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_servers: dict | None = None,
    browser: bool = False,
    cwd: str | None = None,
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

    # Persistent memory: guidance + a frozen snapshot of MEMORY.md/USER.md.
    # Snapshot at session start keeps the system prompt stable (prompt cache);
    # mid-session memory writes land on disk and appear next session.
    memory_block = MEMORY_GUIDANCE + MemoryStore().snapshot()

    return ClaudeAgentOptions(
        model=config.llm.model or None,   # empty => engine/subscription default
        system_prompt=SYSTEM_PROMPT + (BROWSER_GUIDANCE if browser else "") + memory_block,
        # The gate runs as a PreToolUse hook so it fires for EVERY tool call,
        # regardless of permission mode (can_use_tool only fires when the engine
        # decides a prompt is needed, which it often doesn't). A PostToolUse hook
        # captures page snapshots so the gate can read button labels.
        hooks=hooks,
        permission_mode="default",
        # allowed_tools is a pre-approval list — leaving it empty keeps every
        # tool subject to the hook. disallowed_tools blocks tools outright.
        allowed_tools=allowed_tools if allowed_tools is not None else [],
        disallowed_tools=disallowed_tools or [],
        mcp_servers=mcp_servers or {},
        setting_sources=[],              # gate is the sole authority
        # Browser snapshots of heavy web apps (Gmail, etc.) can exceed the 1 MB
        # default and break the control stream; give the buffer generous room.
        max_buffer_size=64 * 1024 * 1024,
        env=env,
        cwd=cwd or os.getcwd(),
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
        disallowed_tools: list[str] | None = None,
        mcp_servers: dict | None = None,
        browser: bool = False,
        curate: bool = True,
        cwd: str | None = None,
    ):
        self.config = config
        self.audit = audit or AuditLog("neovis_audit.db")
        self.automode = AutoMode()
        self.approval = approval
        self.page = PageContext()
        self.actor = actor
        self.session_id = session_id
        self.transcript = ConversationLog()
        self._curate = curate
        self.curator = None
        self._bg: set = set()
        pre_hook = build_pre_tool_use_hook(
            config.policy, self.audit, approval, self.automode,
            page=self.page, actor=actor, session_id=session_id,
        )
        post_hook = build_post_tool_use_hook(self.page)
        hooks = {
            "PreToolUse": [HookMatcher(hooks=[pre_hook])],
            "PostToolUse": [HookMatcher(hooks=[post_hook])],
        }
        # Every session gets the persistent-memory + conversation-recall tools
        # alongside whatever servers the caller provides, plus local code
        # intelligence (codebase-memory-mcp) when its binary is installed.
        servers = dict(mcp_servers or {})
        servers.setdefault("neovis-memory", build_memory_mcp(MemoryStore()))
        servers.setdefault("neovis-recall", build_recall_mcp(self.transcript))
        from ..mcp.code import code_intel_mcp

        for key, cfg_entry in code_intel_mcp().items():
            servers.setdefault(key, cfg_entry)
        self.options = build_options(
            config, hooks,
            gateway_url=gateway_url, gateway_key=gateway_key,
            allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
            mcp_servers=servers, browser=browser, cwd=cwd,
        )
        self.client = ClaudeSDKClient(options=self.options)

    async def connect(self) -> None:
        await self.client.connect()
        if self._curate:
            from .curator import MemoryCurator

            self.curator = MemoryCurator()
            await self.curator.connect()  # tolerates failure (available=False)

    async def disconnect(self) -> None:
        if self.curator is not None:
            await self.curator.disconnect()
        await self.client.disconnect()

    async def send(self, message: str, on_tool=None, transcript_text: str | None = None,
                   on_text=None) -> str:
        """Run one top-level turn; auto-mode is scoped to this turn only.

        ``on_tool(name, input)`` is called for each tool the agent invokes, so a
        caller can show a live trace of what Neovis is doing. ``on_text(text)``
        fires for each assistant text block AS IT STREAMS — the voice channel
        narrates these aloud so the user hears progress, not silence.
        ``transcript_text`` overrides what gets recorded for recall (e.g. the
        raw voice transcript without the reply-format scaffolding).
        """
        self.automode.reset()
        await self.client.query(message)
        parts: list[str] = []
        async for msg in self.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                        if on_text:
                            on_text(block.text)
                    elif isinstance(block, ToolUseBlock) and on_tool:
                        on_tool(block.name, block.input)
            elif isinstance(msg, ResultMessage):
                break
        reply = "\n".join(p.strip() for p in parts if p.strip())

        # Long-term recall: every turn lands in the FTS transcript store.
        logged = transcript_text or message
        try:
            self.transcript.record(self.session_id, self.actor, "user", logged)
            self.transcript.record(self.session_id, self.actor, "assistant", reply)
        except Exception:
            pass
        # Self-learning: hand the finished turn to the background curator.
        if self.curator is not None:
            self.curator.review_later(logged, reply)
        return reply

    def stop(self) -> None:
        # cooperative interrupt of the in-flight task
        import asyncio

        asyncio.ensure_future(self.client.interrupt())
