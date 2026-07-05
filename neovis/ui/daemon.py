"""The background daemon the GUI starts/stops.

Runs the two ways you talk to Neovis, in one place, on a background thread with
its own asyncio loop:
  • the Slack channel (phone), and
  • the global push-to-talk / hands-free voice loop (desk).

The GUI never blocks: start() spawns the thread, stop() unwinds it, and status
updates arrive via a callback.
"""

from __future__ import annotations

import asyncio
import os
import threading


class NeovisDaemon:
    def __init__(self, on_status=None, approval_factory=None):
        self._on_status = on_status              # callable(dict)
        self._approval_factory = approval_factory  # () -> ApprovalGateway for voice
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Future | None = None
        self.status = {"running": False, "slack": False, "voice": False, "error": ""}

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: dict) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(target=self._thread_main, args=(dict(settings),), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop and self._stop and not self._stop.done():
            self._loop.call_soon_threadsafe(self._stop.set_result, None)

    # ── internals ────────────────────────────────────────────────────────────
    def _emit(self) -> None:
        if self._on_status:
            self._on_status(dict(self.status))

    def _thread_main(self, settings: dict) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self.status.update(running=True, error="")
        self._emit()
        try:
            loop.run_until_complete(self._main(settings))
        except Exception as exc:  # surface to the GUI
            self.status["error"] = str(exc)
        finally:
            self.status.update(running=False, slack=False, voice=False)
            self._emit()
            loop.close()

    async def _main(self, settings: dict) -> None:
        self._stop = asyncio.get_event_loop().create_future()
        tasks = []
        if settings.get("slack_bot_token") and settings.get("slack_app_token"):
            os.environ["SLACK_BOT_TOKEN"] = settings["slack_bot_token"]
            os.environ["SLACK_APP_TOKEN"] = settings["slack_app_token"]
            tasks.append(asyncio.create_task(self._run_slack()))
        tasks.append(asyncio.create_task(self._run_voice(settings)))
        try:
            await self._stop
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_slack(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        from ..channels.slack.app import build_slack_app

        app = build_slack_app()
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        self.status["slack"] = True
        self._emit()
        try:
            await handler.start_async()
        finally:
            self.status["slack"] = False
            self._emit()

    async def _run_voice(self, settings: dict) -> None:
        from ..channels.desktop.voice import build_voice_loop

        approval = self._approval_factory() if self._approval_factory else None
        loop, cleanup, _ = await build_voice_loop(
            voice=settings.get("voice", "sky"),
            hotwords=settings.get("hotwords") or None,
            approval=approval,
        )
        self.status["voice"] = True
        self._emit()
        try:
            if settings.get("hands_free"):
                await loop.run_hands_free()
            else:
                await loop.run_push_to_talk(key_name=settings.get("hotkey", "cmd_r"))
        finally:
            self.status["voice"] = False
            self._emit()
            await cleanup()
