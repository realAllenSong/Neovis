"""Slack channel — command Neovis from your phone via Socket Mode.

Socket Mode means no inbound port or public URL: the daemon dials out to Slack,
so it works from behind a corporate firewall. This is the phone entry point;
the desktop entry point (hotkey/voice) reuses the same agent core.

Run:  python -m neovis.channels.slack.app
Env:  SLACK_BOT_TOKEN (xoxb-…), SLACK_APP_TOKEN (xapp-…). Model auth is the
      session's concern (subscription Claude / proxy / gateway).

Each Slack user gets a NeovisSession (Claude Agent SDK) whose tool use is
consequence-gated exactly like the desktop REPL. The interactive approval card
lives in :mod:`.blocks`; :class:`SlackApproval` bridges those buttons to the
approval gateway the gate awaits — so an OUTWARD action pauses on the user's
phone with Approve / Deny.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from uuid import uuid4

from ...core.approval import ApprovalDecision, ApprovalGateway, ApprovalRequest
from ...core.audit import AuditLog
from ...core.config import AppConfig, load_config
from ...core.session import NeovisSession
from .blocks import approval_blocks, decided_blocks, to_mrkdwn


@dataclass
class _Pending:
    future: "asyncio.Future[ApprovalDecision]"
    tool: str
    risk: str
    channel: str
    ts: str | None = None


# request_id -> pending approval, shared so any button handler can resolve it.
_PENDING: dict[str, _Pending] = {}


class SlackApproval(ApprovalGateway):
    """Post an approval card to the user's DM and await the button press."""

    def __init__(self, client, channel: str, timeout: float = 300.0):
        self._client = client
        self._channel = channel
        self._timeout = timeout

    async def request(self, req: ApprovalRequest) -> ApprovalDecision:
        rid = uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalDecision] = loop.create_future()
        pending = _Pending(future=fut, tool=req.tool, risk=req.risk, channel=self._channel)
        _PENDING[rid] = pending

        posted = await self._client.chat_postMessage(
            channel=self._channel,
            blocks=approval_blocks(rid, req.tool, req.args, req.risk),
            text=f"Approval needed: {req.tool}",
        )
        pending.ts = posted.get("ts")

        try:
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            _PENDING.pop(rid, None)
            return ApprovalDecision(approved=False, reason="approval timed out")


# ── session management ────────────────────────────────────────────────────────
# One conversation per Slack user, each with an approval gateway bound to that DM
# so approval cards land in *that* user's DM (their phone).
_SESSIONS: dict[str, NeovisSession] = {}


async def _get_session(config: AppConfig, audit: AuditLog, user: str, channel: str, client) -> NeovisSession:
    session = _SESSIONS.get(user)
    if session is None:
        session = NeovisSession(
            config,
            approval=SlackApproval(client, channel),
            audit=audit,
            actor=user,
            session_id=f"slack:{user}",
        )
        await session.connect()
        _SESSIONS[user] = session
    return session


def _artifact_paths(text: str) -> list[str]:
    """Existing file paths mentioned in a tool result (screenshots, zips)."""
    seen: list[str] = []
    for candidate in re.findall(r"(/[^\s()]+\.(?:png|jpg|jpeg|zip|pdf|csv|xlsx))", text):
        if os.path.isfile(candidate) and candidate not in seen:
            seen.append(candidate)
    return seen


def _format_step(name: str, tool_input: dict) -> str:
    """A one-line 'what Neovis is doing' step for the live Slack trace."""
    short = name.rsplit("__", 1)[-1]
    for key in ("url", "command", "file_path", "path", "query", "pattern", "uid", "value"):
        if tool_input.get(key):
            return f"{short} `{str(tool_input[key])[:60]}`"
    return short


def build_slack_app():
    """Construct the AsyncApp with all handlers wired. Requires the 'slack' extra."""
    from slack_bolt.async_app import AsyncApp

    config = load_config()
    audit = AuditLog(os.environ.get("NEOVIS_AUDIT_DB", "neovis_audit.db"))
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("message")
    async def on_message(event, client, say):  # noqa: ANN001
        if event.get("bot_id") or event.get("subtype"):
            return
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        msg_ts = event.get("ts")
        if not user or not text:
            return

        if text.lower() in ("stop", "/stop"):
            if (s := _SESSIONS.get(user)) is not None:
                s.stop()
            await say("🛑 Stopping the current task.")
            return

        # 👀 acknowledge, then a live "working" message that shows each step.
        try:
            await client.reactions_add(channel=channel, timestamp=msg_ts, name="eyes")
        except Exception:
            pass
        status = await client.chat_postMessage(channel=channel, text="🤔  _Neovis is on it…_")
        status_ts = status["ts"]

        steps: list[str] = []

        def on_tool(name, tool_input):
            steps.append(_format_step(name, tool_input))

        async def show_progress():
            shown = -1
            while True:
                await asyncio.sleep(1.0)
                if len(steps) != shown:
                    shown = len(steps)
                    trace = "\n".join(f"• {s}" for s in steps[-10:])
                    try:
                        await client.chat_update(
                            channel=channel, ts=status_ts,
                            text=(f"🔧  _working…_\n{trace}" if trace else "🤔  _Neovis is on it…_"),
                        )
                    except Exception:
                        pass

        updater = asyncio.create_task(show_progress())
        session = await _get_session(config, audit, user, channel, client)
        try:
            output = await session.send(text, on_tool=on_tool)
        except Exception as exc:  # keep the channel alive on any tool/model error
            updater.cancel()
            await client.chat_update(channel=channel, ts=status_ts, text=f"⚠️ Error: {exc}")
            return
        updater.cancel()

        # Replace the working message with the final answer, rendered for Slack.
        await client.chat_update(channel=channel, ts=status_ts, text=to_mrkdwn(output) or "_(done)_")
        try:
            await client.reactions_remove(channel=channel, timestamp=msg_ts, name="eyes")
            await client.reactions_add(channel=channel, timestamp=msg_ts, name="white_check_mark")
        except Exception:
            pass
        for path in _artifact_paths(output):
            try:
                await client.files_upload_v2(channel=channel, file=path,
                                             title=os.path.basename(path))
            except Exception:
                pass

    async def _resolve(action, body, client, approved: bool):
        rid = action["value"]
        pending = _PENDING.pop(rid, None)
        approver = body["user"]["id"]
        if pending is not None:
            try:
                await client.chat_update(
                    channel=pending.channel,
                    ts=pending.ts,
                    blocks=decided_blocks(pending.tool, pending.risk, approved, approver),
                    text="decision recorded",
                )
            except Exception:
                pass
            if not pending.future.done():
                pending.future.set_result(
                    ApprovalDecision(
                        approved=approved,
                        approver=approver if approved else None,
                        reason=None if approved else f"denied by {approver}",
                    )
                )

    @app.action("neovis_approve")
    async def on_approve(ack, body, action, client):  # noqa: ANN001
        await ack()
        await _resolve(action, body, client, approved=True)

    @app.action("neovis_deny")
    async def on_deny(ack, body, action, client):  # noqa: ANN001
        await ack()
        await _resolve(action, body, client, approved=False)

    return app


async def _run() -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    # Only the Slack tokens are strictly required here — the model auth is the
    # session's concern (subscription Claude, a proxy, or a gateway key).
    missing = [v for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
               if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

    app = build_slack_app()
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("Neovis Slack channel online (Socket Mode). DM the bot from your phone.")
    await handler.start_async()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
