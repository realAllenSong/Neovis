"""The background daemon the GUI starts/stops.

Runs the two ways you talk to Neovis, in one place, on a background thread with
its own asyncio loop:
  • the Slack channel (phone), and
  • the global push-to-talk / hands-free voice loop (desk).

The GUI never blocks: start() spawns the thread, stop() unwinds it, and status
updates arrive via a callback. Each channel is isolated — if voice can't come up
(no mic, missing model), Slack still runs, and the reason is shown in the app
instead of vanishing.

Status shape (all keys always present):
    {"running": bool,
     "slack": "off"|"starting"|"on"|"error",  "slack_msg": str,
     "voice": "off"|"starting"|"on"|"error",  "voice_msg": str}
"""

from __future__ import annotations

import asyncio
import os
import threading


def _friendly(exc: Exception) -> str:
    """Turn a raw startup exception into one short human line."""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "portaudio" in low or "no default input" in low or "invalid number of channels" in low:
        return "no microphone found"
    if "authentication" in low or "oauth" in low or "credentials" in low or "401" in low:
        return "Claude sign-in needed"
    return text.splitlines()[0][:60]


class NeovisDaemon:
    def __init__(self, on_status=None, approval_factory=None):
        self._on_status = on_status              # callable(dict)
        self._approval_factory = approval_factory  # () -> ApprovalGateway for voice
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Future | None = None
        self.status = {
            "running": False,
            "slack": "off", "slack_msg": "",
            "voice": "off", "voice_msg": "",
        }

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, settings: dict) -> None:
        if self.is_running():
            return
        self._thread = threading.Thread(
            target=self._thread_main, args=(dict(settings),), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._loop and self._stop and not self._stop.done():
            self._loop.call_soon_threadsafe(self._stop.set_result, None)

    # ── internals ────────────────────────────────────────────────────────────
    def _set(self, **kw) -> None:
        self.status.update(kw)
        if self._on_status:
            self._on_status(dict(self.status))

    def _thread_main(self, settings: dict) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._set(running=True, slack="off", slack_msg="", voice="off", voice_msg="")
        try:
            loop.run_until_complete(self._main(settings))
        except Exception as exc:  # only unexpected control-plane failures reach here
            self.status["voice_msg"] = self.status["voice_msg"] or _friendly(exc)
        finally:
            self._set(running=False, slack="off", voice="off")
            loop.close()
            self._loop = None

    async def _main(self, settings: dict) -> None:
        self._stop = asyncio.get_event_loop().create_future()
        tasks = []
        has_slack = bool(settings.get("slack_bot_token") and settings.get("slack_app_token"))
        if has_slack:
            os.environ["SLACK_BOT_TOKEN"] = settings["slack_bot_token"]
            os.environ["SLACK_APP_TOKEN"] = settings["slack_app_token"]
            tasks.append(asyncio.create_task(self._run_slack()))
        else:
            self._set(slack="off", slack_msg="add tokens below")
        tasks.append(asyncio.create_task(self._run_voice(settings)))
        try:
            await self._stop
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_slack(self) -> None:
        self._set(slack="starting", slack_msg="")
        try:
            from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

            from ..channels.slack.app import build_slack_app

            app = build_slack_app()
            handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set(slack="error", slack_msg=_friendly(exc))
            return
        self._set(slack="on", slack_msg="")
        try:
            await handler.start_async()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._set(slack="error", slack_msg=_friendly(exc))
        finally:
            if self.status["slack"] == "on":
                self._set(slack="off")

    async def _run_voice(self, settings: dict) -> None:
        self._set(voice="starting", voice_msg="loading…")
        cleanup = None
        try:
            from ..channels.desktop.voice import build_voice_loop

            approval = self._approval_factory() if self._approval_factory else None
            loop, cleanup, _ = await build_voice_loop(
                voice=settings.get("voice", "sky"),
                hotwords=settings.get("hotwords") or None,
                approval=approval,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set(voice="error", voice_msg=_friendly(exc))
            return
        self._set(voice="on", voice_msg="")
        try:
            if settings.get("hands_free"):
                await loop.run_hands_free()
            else:
                await loop.run_push_to_talk(key_name=settings.get("hotkey", "cmd_r"))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._set(voice="error", voice_msg=_friendly(exc))
        finally:
            if self.status["voice"] == "on":
                self._set(voice="off")
            if cleanup is not None:
                try:
                    await cleanup()
                except Exception:
                    pass
