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
from ...core.session import NeovisSession
from ...voice.asr import TransducerASR
from ...voice.commands import parse_voice_command
from ...voice.tts import VOICES, KokoroTTS


def _play(wav: str) -> None:
    if sys.platform == "darwin":
        cmd = ["afplay", wav]
    elif shutil.which("aplay"):
        cmd = ["aplay", "-q", wav]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav]
    else:
        print("(no audio player found — install ffmpeg or aplay)")
        return
    subprocess.run(cmd)


def _for_speech(text: str, limit: int = 600) -> str:
    t = re.sub(r"[*_`#>|]+", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] if t else "Done."


class VoiceLoop:
    def __init__(self, session: NeovisSession, asr: TransducerASR, tts: KokoroTTS):
        self.session = session
        self.asr = asr
        self.tts = tts
        self._wav = Path(tempfile.gettempdir()) / "neovis_say.wav"

    def speak(self, text: str) -> None:
        self.tts.synthesize(text, self._wav)
        _play(str(self._wav))

    async def handle_utterance(self, text: str) -> str:
        # 1) Voice command Neovis handles itself (easter egg: accent/gender).
        vc = parse_voice_command(text)
        if vc is not None:
            _, name = vc
            self.tts.set_voice(name=name)
            _, accent, gender = (name,) + VOICES[name][1:]
            self.speak(f"Okay, switching to a {accent} {gender} voice.")
            return f"(voice → {name})"
        # 2) Otherwise it's a task for the gated agent.
        reply = await self.session.send(text)
        self.speak(_for_speech(reply))
        return reply

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


async def _run(args) -> int:
    try:
        config = load_config()
        config.llm.model = config.llm.model or ""
    except Exception:
        config = AppConfig(llm=ModelConfig(provider="anthropic", model=""))

    session = NeovisSession(
        config, approval=ConsoleApproval(), audit=AuditLog(args.audit_db),
        actor="voice", session_id="desktop-voice",
    )
    await session.connect()
    asr = TransducerASR(hotwords=args.hotword or None)
    tts = KokoroTTS(voice=args.voice)
    loop = VoiceLoop(session, asr, tts)
    try:
        if args.type:
            await loop.run_typed()
        else:
            await loop.run_push_to_talk(key_name=args.key)
    finally:
        await session.disconnect()
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="neovis-voice", description="Desktop voice assistant.")
    p.add_argument("--type", action="store_true", help="type instead of speaking (no mic)")
    p.add_argument("--key", default="cmd_r", help="push-to-talk key (cmd_r/alt_r/ctrl_r)")
    p.add_argument("--voice", default="sky", help="Kokoro voice: sky/adam/emma/george")
    p.add_argument("--hotword", action="append", help="ASR hotword (repeatable)")
    p.add_argument("--audit-db", default="neovis_audit.db")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
