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


class VoiceLoop:
    def __init__(self, session: NeovisSession, asr: TransducerASR, tts: KokoroTTS, router=None):
        self.session = session
        self.asr = asr
        self.tts = tts
        self.router = router  # IntentRouter (Haiku) or None → rule fallback
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
        if self.router is not None:
            intent = await self.router.classify(text)
        else:
            from ...core.router import rule_fallback

            intent = rule_fallback(text)
        action = intent.get("action", "task")

        if action == "stop":
            self.session.stop()
            self.speak("Stopping.")
            return "(stopped)"
        if action == "voice":
            return self._switch_voice(intent.get("name"), intent.get("accent"), intent.get("gender"))
        # task → the gated agent
        reply = await self.session.send(text)
        self.speak(_for_speech(reply))
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
        import numpy as np
        import sounddevice as sd
        from pynput import keyboard

        ptt = getattr(keyboard.Key, key_name, None)
        if ptt is None:
            print(f"Unknown hotkey {key_name!r}; use e.g. cmd_r, alt_r, ctrl_r.")
            return

        frames: list = []
        state = {"recording": False}

        def audio_cb(indata, _n, _t, _s):
            if state["recording"]:
                frames.append(indata.copy())

        stream = sd.InputStream(samplerate=samplerate, channels=1, dtype="float32", callback=audio_cb)
        stream.start()
        loop = asyncio.get_event_loop()
        print(f"Push-to-talk: hold [{key_name}] and speak; release to send. Esc quits.")

        pending: list = []

        def on_press(k):
            if k == ptt and not state["recording"]:
                frames.clear()
                state["recording"] = True
                print("● listening…")

        def on_release(k):
            if k == ptt and state["recording"]:
                state["recording"] = False
                if frames:
                    audio = np.concatenate(frames, axis=0).flatten()
                    text = self.asr.transcribe_samples(audio, samplerate)
                    print("you:", text or "(nothing heard)")
                    if text.strip():
                        pending.append(text)
            if k == keyboard.Key.esc:
                return False

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        try:
            while listener.running:
                if pending:
                    text = pending.pop(0)
                    print("neovis>", await self.handle_utterance(text))
                await asyncio.sleep(0.05)
        finally:
            listener.stop()
            stream.stop()

    async def run_hands_free(self, samplerate: int = 16000) -> None:
        """Continuous VAD-segmented listening with barge-in: no key to hold, and
        speaking stops the moment you start talking. Best with headphones — a
        speaker's audio leaking into the mic will trip the barge-in (echo)."""
        import queue

        import sounddevice as sd

        from ...voice.vad import VAD

        vad = VAD(sample_rate=samplerate)
        self._speak_blocking = False  # keep listening while Neovis talks
        q: "queue.Queue" = queue.Queue()

        def audio_cb(indata, _n, _t, _s):
            q.put(indata.copy())

        stream = sd.InputStream(
            samplerate=samplerate, channels=1, dtype="float32", blocksize=512, callback=audio_cb
        )
        stream.start()
        print("Hands-free: just talk (headphones recommended for barge-in). Ctrl-C to quit.")
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(q.get, True, 0.2)
                except queue.Empty:
                    continue
                frames = chunk.flatten()
                vad.accept(frames)
                if self.speaking and vad.is_speaking():
                    self.stop_speaking()  # barge-in
                for seg in vad.segments():
                    text = self.asr.transcribe_samples(seg, samplerate)
                    if text.strip():
                        print("you:", text)
                        print("neovis>", await self.handle_utterance(text))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stream.stop()


async def _run(args) -> int:
    try:
        config = load_config()
        config.llm.model = config.llm.model or ""
    except Exception:
        config = AppConfig(llm=ModelConfig(provider="anthropic", model=""))

    tts = KokoroTTS(voice=args.voice)

    # Proactive watcher: speak aloud when a background job finishes.
    _watch_wav = Path(tempfile.gettempdir()) / "neovis_watch.wav"

    async def _notify(result):
        line = f"Your job {result.note or 'in the background'} is done."
        try:
            tts.synthesize(line, _watch_wav)
            _play(str(_watch_wav))
        except Exception:
            pass

    manager = WatchManager(_notify)
    session = NeovisSession(
        config, approval=ConsoleApproval(), audit=AuditLog(args.audit_db),
        actor="voice", session_id="desktop-voice",
        mcp_servers={"neovis-watch": build_watch_mcp(manager, policy=config.policy)},
    )
    await session.connect()
    asr = TransducerASR(hotwords=args.hotword or None)
    router = IntentRouter()
    await router.connect()
    if router.available:
        print("Intent router: Haiku (fast tier)")
    else:
        print("Intent router: rule-based fallback (no model reachable)")
    loop = VoiceLoop(session, asr, tts, router=router)
    try:
        if args.type:
            await loop.run_typed()
        elif args.hands_free:
            await loop.run_hands_free()
        else:
            await loop.run_push_to_talk(key_name=args.key)
    finally:
        await manager.stop()
        await router.disconnect()
        await session.disconnect()
    return 0


def _load_settings() -> dict:
    """Editable defaults at ~/.neovis/settings.yaml (CLI flags override)."""
    import yaml

    path = Path.home() / ".neovis" / "settings.yaml"
    try:
        return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    except Exception:
        return {}


def main() -> None:
    st = _load_settings()
    p = argparse.ArgumentParser(prog="neovis-voice", description="Desktop voice assistant.")
    p.add_argument("--type", action="store_true", help="type instead of speaking (no mic)")
    p.add_argument("--hands-free", action="store_true",
                   help="continuous VAD listening with barge-in (no key; headphones recommended)")
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
