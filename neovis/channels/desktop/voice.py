"""Desktop voice entry — the Siri-style side of Neovis.

Hold a hotkey, speak, release. Neovis transcribes locally (hotword ASR), either
handles a voice command itself (e.g. "switch to a British male accent") or sends
the request to the gated agent, then speaks the reply with Kokoro. All voice I/O
is on-device; only the LLM brain is remote.

    python -m neovis.channels.desktop.voice            # push-to-talk (needs a mic)
    python -m neovis.channels.desktop.voice --type     # type instead of speak (no mic)

Consequential actions still pause for approval at the console.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ...core.approval import ConsoleApproval
from ...core.audit import AuditLog
from ...core.config import AppConfig, ModelConfig, load_config
from ...core.router import IntentRouter
from ...core.session import NeovisSession
from ...core.watch import WatchManager, build_watch_mcp
from ...voice.asr import TransducerASR
from ...voice.tts import VOICES, KokoroTTS


def _play_cmd(wav: str) -> list[str] | None:
    """A subprocess player command (interruptible), or None to fall back."""
    if sys.platform == "darwin":
        return ["afplay", wav]
    for player in (
        ["aplay", "-q", wav],
        ["paplay", wav],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav],
    ):
        if shutil.which(player[0]):
            return player
    return None


def _play(wav: str) -> None:
    cmd = _play_cmd(wav)
    if cmd:
        subprocess.run(cmd)
        return
    if sys.platform.startswith("win"):
        try:
            import winsound

            winsound.PlaySound(wav, winsound.SND_FILENAME)
            return
        except Exception:
            pass
    print("(no audio player found — install ffmpeg, alsa-utils, or pulseaudio)")


def _for_speech(text: str, limit: int = 600) -> str:
    t = re.sub(r"[*_`#>|]+", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] if t else "Done."


class VoiceUI:
    """UI hooks for the voice loop. The desktop overlay implements these; the
    default is a silent no-op so the CLI works unchanged. Methods may be called
    from audio/hotkey threads — implementations must be thread-safe."""

    def listening(self) -> None: ...
    def level(self, rms: float) -> None: ...
    def thinking(self, text: str) -> None: ...
    def step(self, line: str) -> None: ...
    def response(self, spoken: str, detail: str) -> None: ...
    def error(self, msg: str) -> None: ...
    def idle(self) -> None: ...


# Appended to every voice utterance sent to the agent: the transcript is lossy,
# and the reply must lead with one short speakable line (detail after '---').
VOICE_REPLY_NOTES = (
    "\n\n[Voice notes: the message above is a speech transcript and may contain "
    "recognition errors — interpret it charitably before acting (e.g. 'we chat' "
    "likely means WeChat). Answer for voice: the FIRST line of your reply must "
    "be ONE short conversational sentence (under 18 words, no markdown) to be "
    "read aloud. If more detail is useful, put it after a line containing only "
    "'---'; it will be shown on screen, not spoken.]"
)


def split_reply(reply: str) -> tuple[str, str]:
    """(spoken, detail) from an agent reply. Prefers the '---' protocol, then
    first-line, then a trimmed fallback — never returns an unspeakable wall."""
    text = (reply or "").strip()
    if not text:
        return "Done.", ""
    if "\n---\n" in text:
        head, detail = text.split("\n---\n", 1)
    elif "\n" in text:
        head, detail = text.split("\n", 1)
    else:
        head, detail = text, ""
    spoken = _for_speech(head, limit=240)
    if not spoken or spoken == "Done.":
        spoken = _for_speech(text, limit=240)
    return spoken, detail.strip()


def _format_step(name: str, tool_input: dict) -> str:
    """One short 'what I'm doing' line for the overlay ticker."""
    short = name.rsplit("__", 1)[-1]
    for key in ("url", "command", "file_path", "path", "query", "pattern", "uid", "value"):
        if tool_input.get(key):
            return f"{short}  {str(tool_input[key])[:44]}"
    return short


class VoiceLoop:
    def __init__(self, session: NeovisSession, asr: TransducerASR, tts: KokoroTTS,
                 router=None, ui: VoiceUI | None = None):
        self.session = session
        self.asr = asr
        self.tts = tts
        self.router = router  # IntentRouter (Haiku) or None → rule fallback
        self.ui = ui or VoiceUI()
        self._wav = Path(tempfile.gettempdir()) / "neovis_say.wav"
        self._playing = None          # subprocess.Popen while speaking (barge-in)
        self._speak_blocking = True   # hands-free flips this so we keep listening

    def speak(self, text: str, *, blocking: bool | None = None) -> None:
        if blocking is None:
            blocking = self._speak_blocking
        self.tts.synthesize(text, self._wav)
        cmd = _play_cmd(str(self._wav))
        if cmd is None:
            _play(str(self._wav))     # blocking fallback (e.g. Windows winsound)
            return
        if blocking:
            subprocess.run(cmd)
        else:
            self._playing = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def speaking(self) -> bool:
        return self._playing is not None and self._playing.poll() is None

    def stop_speaking(self) -> None:
        if self._playing is not None and self._playing.poll() is None:
            self._playing.terminate()
        self._playing = None

    async def handle_utterance(self, text: str) -> str:
        # The fast model (Haiku) decides what this means; the big model executes.
        from ...core.router import _fast_rules, rule_fallback

        self.ui.thinking(text)
        intent = _fast_rules(text) if self.router is not None else rule_fallback(text)
        if intent is None:
            # Almost certainly a task → acknowledge INSTANTLY (JARVIS-style) so
            # the user knows they were heard while the slower brains spin up.
            self.speak("On it.", blocking=False)
            intent = await self.router.classify(text)
        action = intent.get("action", "task")

        if action == "stop":
            self.session.stop()
            self.ui.response("Stopping.", "")
            self.speak("Stopping.")
            return "(stopped)"
        if action == "voice":
            out = self._switch_voice(intent.get("name"), intent.get("accent"), intent.get("gender"))
            self.ui.idle()
            return out
        # task → the gated agent, with a live step ticker on the overlay
        reply = await self.session.send(
            text + VOICE_REPLY_NOTES,
            on_tool=lambda name, tool_input: self.ui.step(_format_step(name, tool_input)),
            transcript_text=text,  # recall stores the raw utterance, not the scaffolding
        )
        spoken, detail = split_reply(reply)
        self.ui.response(spoken, detail)   # show first, then talk
        self.speak(spoken)
        return reply

    def _switch_voice(self, name: str | None, accent: str | None, gender: str | None) -> str:
        """Fill the missing dimension from the current voice and announce; if
        nothing usable was given, ask which voice."""
        _, cur_accent, cur_gender = (self.tts.voice_name,) + VOICES[self.tts.voice_name][1:]

        if name:
            self.tts.set_voice(name=name)
            return self._announce_voice(inferred=False)

        if not accent and not gender:
            self.speak(
                "Sure. I can do American or British, male or female. "
                f"You're on {cur_accent} {cur_gender} now. Which would you like?"
            )
            return "(asked which voice)"

        self.tts.set_voice(accent=accent or cur_accent, gender=gender or cur_gender)
        return self._announce_voice(inferred=not (accent and gender))

    def _announce_voice(self, *, inferred: bool) -> str:
        _, accent, gender = (self.tts.voice_name,) + VOICES[self.tts.voice_name][1:]
        msg = f"Okay — {accent} {gender} voice now."
        if inferred:
            msg += " Say American or British, or male or female, to change it."
        self.speak(msg)
        return f"(voice → {self.tts.voice_name})"

    # ── input modes ──────────────────────────────────────────────────────────
    async def run_typed(self) -> None:
        print(f"Voice loop (typed). Current voice: {self.tts.voice_name}. /quit to exit.")
        while True:
            try:
                line = (await asyncio.to_thread(input, "say> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            print("neovis>", await self.handle_utterance(line))

    async def run_push_to_talk(self, key_name: str = "cmd_r", samplerate: int = 16000) -> None:
        from collections import deque

        import numpy as np
        import sounddevice as sd

        from .hotkey import HotkeyListener

        frames: list = []
        state = {"recording": False}
        pending_audio: list = []  # raw clips; ASR runs in this loop, never in a callback
        # ~0.5 s rolling pre-buffer: people start talking a beat before (or as)
        # they press the key — without this the first word gets swallowed.
        preroll: deque = deque(maxlen=8)

        def audio_cb(indata, _n, _t, _s):
            if state["recording"]:
                frames.append(indata.copy())
                self.ui.level(float(np.sqrt((indata ** 2).mean())))
            else:
                preroll.append(indata.copy())

        def on_press():
            if self.speaking:
                self.stop_speaking()  # pressing the key interrupts Neovis
            frames.clear()
            frames.extend(preroll)
            state["recording"] = True
            self.ui.listening()
            print("● listening…")

        def on_release():
            state["recording"] = False
            if frames:
                pending_audio.append(np.concatenate(frames, axis=0).flatten())

        listener = HotkeyListener(key_name, on_press, on_release)
        listener.start()  # raises with a friendly message if the OS denies the tap
        stream = sd.InputStream(samplerate=samplerate, blocksize=1024, channels=1,
                                dtype="float32", callback=audio_cb)
        stream.start()
        print(f"Push-to-talk: hold [{key_name}] and speak; release to send. Ctrl-C quits.")
        try:
            while True:
                if pending_audio:
                    self.ui.thinking("…")
                    text = self.asr.transcribe_samples(pending_audio.pop(0), samplerate)
                    print("you:", text or "(nothing heard)")
                    if text.strip():
                        print("neovis>", await self.handle_utterance(text))
                    else:
                        self.ui.error("Didn't catch that — try again?")
                await asyncio.sleep(0.05)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            listener.stop()
            stream.stop()

    async def run_hands_free(self, samplerate: int = 16000, barge_in: bool = False) -> None:
        """Continuous VAD-segmented listening: no key to hold.

        barge_in=False (default): the mic is IGNORED while Neovis speaks — on
        open speakers its own voice would otherwise trip the VAD, kill playback
        instantly, and get transcribed as ghost input (no echo cancellation).
        barge_in=True (headphones): talk over Neovis to cut it off.
        """
        import queue
        import time as _time

        import numpy as np
        import sounddevice as sd

        from ...voice.vad import VAD

        vad = VAD(sample_rate=samplerate)
        self._speak_blocking = False  # keep the loop alive while Neovis talks
        q: "queue.Queue" = queue.Queue()

        def audio_cb(indata, _n, _t, _s):
            q.put(indata.copy())

        stream = sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32", blocksize=512, callback=audio_cb
        )
        stream.start()
        mode = "barge-in on (headphones)" if barge_in else "mic muted while Neovis speaks"
        print(f"Hands-free: just talk — {mode}. Ctrl-C to quit.")
        was_speaking = False
        quiet_until = 0.0
        in_speech = False
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(q.get, True, 0.2)
                except queue.Empty:
                    continue
                frames = chunk.flatten()

                if not barge_in:
                    if self.speaking:
                        was_speaking = True
                        continue  # drop mic input entirely while talking
                    if was_speaking:
                        was_speaking = False
                        vad.reset()  # discard anything captured around playback
                        quiet_until = _time.time() + 0.35  # echo tail
                        continue
                    if _time.time() < quiet_until:
                        continue

                vad.accept(frames)
                self.ui.level(float(np.sqrt((frames ** 2).mean())))
                if barge_in and self.speaking and vad.is_speaking():
                    self.stop_speaking()  # talk over Neovis to cut it off
                if vad.is_speaking() and not in_speech:
                    in_speech = True
                    self.ui.listening()
                for seg in vad.segments():
                    in_speech = False
                    text = self.asr.transcribe_samples(seg, samplerate)
                    if text.strip():
                        print("you:", text)
                        print("neovis>", await self.handle_utterance(text))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stream.stop()


async def build_voice_loop(*, voice="sky", hotwords=None, audit_db="neovis_audit.db",
                           approval=None, ui=None):
    """Assemble a connected VoiceLoop (session + gate + ASR + TTS + router +
    watcher). Returns (loop, cleanup, router_available). Reused by the CLI and GUI."""
    try:
        config = load_config()
        config.llm.model = config.llm.model or ""
    except Exception:
        config = AppConfig(llm=ModelConfig(provider="anthropic", model=""))

    tts = KokoroTTS(voice=voice)
    watch_wav = Path(tempfile.gettempdir()) / "neovis_watch.wav"

    async def _notify(result):
        line = f"Your job {result.note or 'in the background'} is done."
        try:
            tts.synthesize(line, watch_wav)
            _play(str(watch_wav))
        except Exception:
            pass

    manager = WatchManager(_notify)
    session = NeovisSession(
        config, approval=approval or ConsoleApproval(), audit=AuditLog(audit_db),
        actor="voice", session_id="desktop-voice",
        mcp_servers={"neovis-watch": build_watch_mcp(manager, policy=config.policy)},
    )
    await session.connect()
    # Hotwords = the user's configured list + every name Neovis remembers
    # (MEMORY.md/USER.md) — memory shapes what the ears hear.
    from ...voice.hotwords import names_from_memory

    phrases = list(dict.fromkeys((hotwords or []) + names_from_memory()))
    from ...voice.asr import build_asr

    asr = build_asr(hotwords=phrases or None)
    router = IntentRouter()
    await router.connect()
    loop = VoiceLoop(session, asr, tts, router=router, ui=ui)

    async def cleanup():
        await manager.stop()
        await router.disconnect()
        await session.disconnect()

    return loop, cleanup, router.available


async def _run(args) -> int:
    loop, cleanup, router_ok = await build_voice_loop(
        voice=args.voice, hotwords=args.hotword, audit_db=args.audit_db
    )
    print("Intent router:", "Haiku (fast tier)" if router_ok else "rule-based fallback")
    try:
        if args.type:
            await loop.run_typed()
        elif args.hands_free:
            await loop.run_hands_free(barge_in=args.barge_in)
        else:
            await loop.run_push_to_talk(key_name=args.key)
    finally:
        await cleanup()
    return 0


def _load_settings() -> dict:
    """Editable defaults at ~/.neovis/settings.yaml (CLI flags override)."""
    from ...core.settings import load

    return load()


def main() -> None:
    st = _load_settings()
    p = argparse.ArgumentParser(prog="neovis-voice", description="Desktop voice assistant.")
    p.add_argument("--type", action="store_true", help="type instead of speaking (no mic)")
    p.add_argument("--hands-free", action="store_true",
                   help="continuous VAD listening (no key to hold)")
    p.add_argument("--barge-in", action="store_true", default=bool(st.get("barge_in")),
                   help="talk over Neovis to interrupt it (headphones only — echo!)")
    p.add_argument("--key", default=st.get("hotkey", "cmd_r"),
                   help="push-to-talk key (cmd_r/alt_r/ctrl_r/f5/…); default from settings.yaml")
    p.add_argument("--voice", default=st.get("voice", "sky"), help="Kokoro voice: sky/adam/emma/george")
    p.add_argument("--hotword", action="append", default=st.get("hotwords"),
                   help="ASR hotword (repeatable)")
    p.add_argument("--audit-db", default="neovis_audit.db")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
