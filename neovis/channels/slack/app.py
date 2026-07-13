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
from pathlib import Path
from uuid import uuid4

from ...core.approval import ApprovalDecision, ApprovalGateway, ApprovalRequest
from ...core.audit import AuditLog
from ...core.config import AppConfig, load_config
from ...core.session import NeovisSession
from ...core.watch import WatchManager, build_watch_mcp
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

# The in-flight turn per user, so a new message can STEER a running task
# (interrupt-and-redirect) instead of colliding with it.
_RUNNING: dict[str, "asyncio.Task"] = {}

_STEER_NOTE = (
    "[The user sent this WHILE you were mid-task — treat it as a live redirect: "
    "drop or adjust the old plan and pivot to this, reusing the context of what "
    "you were doing.]\n"
)


async def shutdown_state() -> None:
    """Release everything bound to the current event loop. MUST be awaited on
    that loop before it closes (the GUI stops/starts Slack across fresh loops;
    a cached session, pending future, or lock from a dead loop poisons the next
    run — and every leaked session is a leaked Claude subprocess)."""
    global _ASR_LOCK
    for task in list(_RUNNING.values()):
        task.cancel()
    _RUNNING.clear()
    for session in list(_SESSIONS.values()):
        try:
            await asyncio.wait_for(session.disconnect(), timeout=5)
        except Exception:
            pass
    _SESSIONS.clear()
    for pending in list(_PENDING.values()):
        if not pending.future.done():
            pending.future.cancel()
    _PENDING.clear()
    _ASR_LOCK = None


async def _get_session(config: AppConfig, audit: AuditLog, user: str, channel: str, client) -> NeovisSession:
    session = _SESSIONS.get(user)
    if session is None:
        async def notify(result):
            emoji = "✅" if result.ok else "⚠️"
            body = f"🔔 *Watch finished* — {result.note or result.target}\n{emoji} {result.detail}"
            try:
                await client.chat_postMessage(channel=channel, text=to_mrkdwn(body))
            except Exception:
                pass

        manager = WatchManager(notify)
        session = NeovisSession(
            config,
            approval=SlackApproval(client, channel),
            audit=audit,
            actor=user,
            session_id=f"slack:{user}",
            mcp_servers={"neovis-watch": build_watch_mcp(manager, policy=config.policy)},
            cwd=str(Path.home()),  # the workstation, not Neovis's own repo
        )
        await session.connect()
        _SESSIONS[user] = session
    return session


# ── voice notes (Slack audio clips → local ASR) ───────────────────────────────
_AUDIO_TYPES = ("m4a", "mp3", "mp4", "wav", "webm", "ogg", "flac", "aac", "amr")
_ASR = None
_ASR_LOCK: asyncio.Lock | None = None


def _audio_files(event: dict) -> list[dict]:
    """Audio attachments on a message (Slack voice memos are audio/mp4 m4a)."""
    out = []
    for f in event.get("files") or []:
        if (f.get("mimetype", "").startswith("audio/")
                or f.get("filetype", "").lower() in _AUDIO_TYPES):
            out.append(f)
    return out


def _to_wav16k(src: "Path") -> "Path":
    """Any audio container → 16 kHz mono wav for the ASR (ffmpeg, or afconvert
    on macOS)."""
    import shutil
    import subprocess
    import sys

    out = src.parent / (src.stem + "_16k.wav")
    if shutil.which("ffmpeg"):
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
             "-loglevel", "error", str(out)],
            check=True,
        )
    elif sys.platform == "darwin" and shutil.which("afconvert"):
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(out)],
            check=True,
        )
    else:
        raise RuntimeError("install ffmpeg to transcribe voice notes")
    return out


async def _get_asr():
    """Load the local ASR once, lazily (Parakeet-TDT via sherpa-onnx)."""
    global _ASR, _ASR_LOCK
    if _ASR_LOCK is None:
        _ASR_LOCK = asyncio.Lock()
    async with _ASR_LOCK:
        if _ASR is None:
            from ...voice.asr import build_asr
            from ...voice.hotwords import names_from_memory

            names = names_from_memory()
            _ASR = await asyncio.to_thread(lambda: build_asr(hotwords=names or None))
    return _ASR


async def _transcribe_slack_audio(f: dict, client) -> str:
    """Download a Slack audio file with the bot token and transcribe locally."""
    import tempfile

    import aiohttp

    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        raise RuntimeError("no downloadable URL on the file")
    suffix = os.path.splitext(f.get("name") or "")[1] or ".m4a"
    raw = Path(tempfile.gettempdir()) / f"neovis_note_{uuid4().hex}{suffix}"
    async with aiohttp.ClientSession() as http:
        async with http.get(url, headers={"Authorization": f"Bearer {client.token}"}) as r:
            body = await r.read()
            ctype = r.headers.get("content-type", "")
            if r.status != 200 or "text/html" in ctype:
                raise RuntimeError(
                    "couldn't download the audio — add the *files:read* scope "
                    "(OAuth & Permissions) and reinstall the app"
                )
    raw.write_bytes(body)
    try:
        wav = await asyncio.to_thread(_to_wav16k, raw)
        asr = await _get_asr()
        return (await asyncio.to_thread(asr.transcribe, wav)).strip()
    finally:
        raw.unlink(missing_ok=True)


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


# ── the thinking indicator ────────────────────────────────────────────────────
# Slack's NATIVE one (assistant.threads.setStatus) renders "Neovis is thinking…"
# as a proper shimmering status OUTSIDE the message, and clears itself the
# moment we reply — that's the real product experience, not a fake status
# message we later overwrite. It needs the app to be an AI app (agent) with the
# assistant:write scope; where that isn't granted we fall back to animating a
# placeholder message ourselves.
_LOADING_MESSAGES = [
    "is thinking…",
    "is reading the screen…",
    "is working on your machine…",
    "is checking things over…",
]


async def set_thinking(client, channel: str, thread_ts: str, status: str) -> bool:
    """Native status. Returns False if this workspace/app can't do it."""
    try:
        await client.assistant_threads_setStatus(
            channel_id=channel, thread_ts=thread_ts,
            status=status, loading_messages=_LOADING_MESSAGES,
        )
        return True
    except Exception:
        return False


async def clear_thinking(client, channel: str, thread_ts: str) -> None:
    try:
        await client.assistant_threads_setStatus(
            channel_id=channel, thread_ts=thread_ts, status="")
    except Exception:
        pass


# Fallback animation (only when the native status isn't available): a braille
# spinner plus a light travelling through a bar, edited into a placeholder.
_SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
_BAR_W = 12


def _shimmer(frame: int, width: int = _BAR_W) -> str:
    head = frame % width
    cells = []
    for i in range(width):
        d = min(abs(i - head), width - abs(i - head))  # wrap-around distance
        cells.append("█" if d == 0 else "▓" if d == 1 else "▒" if d == 2 else "░")
    return "".join(cells)


def thinking_frame(frame: int, steps: list[str] | None = None) -> str:
    """One frame of the 'Neovis is thinking' animation, with the live step
    trace underneath once tools start running."""
    head = f"{_SPINNER[frame % len(_SPINNER)]}  *Neovis is thinking*  `{_shimmer(frame)}`"
    if steps:
        trace = "\n".join(f"• {s}" for s in steps[-8:])
        return f"{head}\n{trace}"
    return head


def build_slack_app():
    """Construct the AsyncApp with all handlers wired. Requires the 'slack' extra."""
    from slack_bolt.async_app import AsyncApp

    config = load_config()
    audit = AuditLog(os.environ.get("NEOVIS_AUDIT_DB", "neovis_audit.db"))
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("message")
    async def on_message(event, client, say):  # noqa: ANN001
        # Accept plain messages AND file_share (voice memos / audio clips).
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        user = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").strip()
        msg_ts = event.get("ts")
        audio = _audio_files(event)
        if not user or (not text and not audio):
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

        if audio and not text:
            # Voice note → transcribe locally first, and show what was heard.
            heard = await client.chat_postMessage(
                channel=channel, text="🎧  _listening to your voice note…_"
            )
            try:
                text = await _transcribe_slack_audio(audio[0], client)
            except Exception as exc:
                await client.chat_update(channel=channel, ts=heard["ts"],
                                         text=f"⚠️ {exc}")
                return
            if not text:
                await client.chat_update(channel=channel, ts=heard["ts"],
                                         text="🎧 I couldn't hear anything in that note.")
                return
            await client.chat_update(channel=channel, ts=heard["ts"],
                                     text=f"🎤  _Heard:_ “{text}”")

        # ── steer: a message while a task is running redirects that task ──────
        running = _RUNNING.get(user)
        if running is not None and not running.done():
            if (s := _SESSIONS.get(user)) is not None:
                s.stop()  # cooperative interrupt of the in-flight turn
            try:
                await client.chat_postMessage(
                    channel=channel, text="🔀  _Redirecting the current task…_"
                )
            except Exception:
                pass
            try:  # let the interrupted turn unwind before starting the new one
                await asyncio.wait_for(asyncio.shield(running), timeout=30)
            except Exception:
                pass
            text = _STEER_NOTE + text

        thread_ts = event.get("thread_ts") or msg_ts
        steps: list[str] = []

        def on_tool(name, tool_input):
            steps.append(_format_step(name, tool_input))

        # Prefer Slack's native status; only fake it if the app lacks the scope.
        native = await set_thinking(client, channel, thread_ts, "is thinking…")
        status_ts = None
        if not native:
            posted = await client.chat_postMessage(channel=channel, text=thinking_frame(0))
            status_ts = posted["ts"]

        async def show_progress():
            """Keep the indicator alive and honest. Native: refresh the status
            with the current step (Slack drops it after 2 min of silence).
            Fallback: animate the placeholder message."""
            frame = 0
            last = -1
            while True:
                await asyncio.sleep(1.0)
                frame += 1
                if native:
                    if len(steps) != last or frame % 30 == 0:  # step change, or keep-alive
                        last = len(steps)
                        detail = steps[-1] if steps else ""
                        await set_thinking(
                            client, channel, thread_ts,
                            f"is working — {detail}"[:100] if detail else "is thinking…")
                    continue
                try:
                    await client.chat_update(
                        channel=channel, ts=status_ts, text=thinking_frame(frame, steps))
                except Exception:
                    pass

        session = await _get_session(config, audit, user, channel, client)

        async def reply(body: str) -> None:
            """Post the answer — replacing the placeholder, or as a fresh
            message in the thread (which clears the native status)."""
            if status_ts:
                await client.chat_update(channel=channel, ts=status_ts, text=body)
            else:
                await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=body)
                await clear_thinking(client, channel, thread_ts)

        async def work() -> None:
            updater = asyncio.create_task(show_progress())
            try:
                output = await session.send(text, on_tool=on_tool)
            except Exception as exc:  # keep the channel alive on any tool/model error
                updater.cancel()
                await reply(f"⚠️ Error: {exc}")
                return
            updater.cancel()

            await reply(to_mrkdwn(output) or "_(done)_")
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

        task = asyncio.create_task(work())
        _RUNNING[user] = task
        try:
            await task
        finally:
            if _RUNNING.get(user) is task:
                _RUNNING.pop(user, None)

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
