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
# interim text is narrated aloud, and the reply must lead with one short
# speakable line (detail after '---').
VOICE_REPLY_NOTES = (
    "\n\n[Voice notes: the message above is a speech transcript and may contain "
    "recognition errors — interpret it charitably before acting (e.g. 'we chat' "
    "likely means WeChat). This is a VOICE conversation: any text you write is "
    "READ ALOUD to the user. While working, before each batch of tool calls, "
    "say ONE short sentence about what you're doing next ('Checking the "
    "repository now.'). When done, your final text must START with one short "
    "conversational sentence summarizing the RESULT itself (the number, the "
    "name, the outcome — not 'done'); optional detail goes after a line "
    "containing only '---' and is shown on screen, not spoken. Casual or "
    "conversational questions deserve a direct conversational answer — do NOT "
    "investigate files or run tools unless the request actually needs the "
    "machine.]"
)


def _first_sentence(text: str, limit: int = 140) -> str:
    """The first sentence of a text block, cleaned for TTS."""
    t = re.sub(r"[*_`#>|]+", "", text or "").strip()
    if not t:
        return ""
    for stop in (". ", "! ", "? ", "\n"):
        idx = t.find(stop)
        if 0 < idx < limit:
            return t[: idx + 1].strip()
    return t[:limit].strip()


# Lone thinking noises that should never become a turn.
_FILLERS = frozenset(
    "um uh er ah oh hmm hm mm mhm huh like so well".split())

# Trailing words that mean the speaker isn't done ("…check the folder and").
_TRAILING_CONNECTIVES = (
    "and", "but", "or", "so", "then", "because", "with", "to", "the", "a",
    "an", "of", "in", "for", "um", "uh", "like",
)


def _utterance_complete(text: str) -> bool:
    """Endpointing heuristic: does this transcript look like a finished
    thought? If not, the mic loop holds ~0.9 s for a continuation and merges
    (GPT-Live 'waits instead of jumping into pauses')."""
    t = (text or "").strip()
    if not t:
        return False
    words = t.split()
    if len(words) <= 2:
        return True  # short commands ("Stop.", "Yes.") dispatch immediately
    last = words[-1].lower().strip(".,!?;:")
    if last in _TRAILING_CONNECTIVES:
        return False
    return t[-1] in ".!?"


_VOICE_STEER_NOTE = (
    "[The user said this WHILE you were mid-task — treat it as a live "
    "redirect: drop or adjust the old plan and pivot, reusing the context of "
    "what you were doing.]\n"
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


class _Trace:
    """White-box timing for one voice turn → ~/.neovis/voice_trace.jsonl +
    one compact console line. This is how we debug 'why was that slow'."""

    PATH = Path.home() / ".neovis" / "voice_trace.jsonl"

    def __init__(self, utterance: str, asr_s: float | None = None):
        import time as _time

        self.t0 = _time.time()
        self.d: dict = {"ts": round(self.t0, 2), "utterance": utterance[:200]}
        if asr_s is not None:
            self.d["asr_s"] = round(asr_s, 2)

    def mark(self, key: str) -> None:
        import time as _time

        self.d[f"{key}_s"] = round(_time.time() - self.t0, 2)

    def done(self, **extra) -> None:
        import json

        self.d.update(extra)
        try:
            self.PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self.PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(self.d, ensure_ascii=False) + "\n")
        except Exception:
            pass
        stages = " ".join(
            f"{k[:-2]}={self.d[k]}s" for k in self.d if k.endswith("_s")
        )
        print(f"⏱ [{self.d.get('action', '?')}] {stages}")


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
        self._last_spoken = ""        # for echo detection (hearing ourselves)
        self._speak_end = 0.0         # when playback last stopped
        self._narrated = ""           # last interim line narrated aloud
        self._narrated_played = False  # whether that line actually got voiced
        self._trace: _Trace | None = None
        self.last_asr_s: float | None = None
        self._bank: dict[str, list] = {}   # pre-synthesized instant clips
        self._turn_task: asyncio.Task | None = None  # the in-flight agent turn
        self._busy_reply = False      # one while-busy exchange at a time

    def _synth(self, text: str) -> Path:
        """Synthesize to a fresh wav (no reuse races between ack/narration/reply)."""
        self._wav_seq = getattr(self, "_wav_seq", 0) + 1
        wav = Path(tempfile.gettempdir()) / f"neovis_say_{self._wav_seq % 8}.wav"
        self._last_spoken = f"{self._last_spoken} {text}"[-400:]  # echo reference
        self.tts.synthesize(text, wav)
        self._wav = wav  # last spoken clip (tests/tools peek at it)
        return wav

    def _play_file(self, wav: Path, *, blocking: bool) -> None:
        import time as _time

        cmd = _play_cmd(str(wav))
        if cmd is None:
            _play(str(wav))           # blocking fallback (e.g. Windows winsound)
            self._speak_end = _time.time()
            return
        if blocking:
            subprocess.run(cmd)
            self._speak_end = _time.time()
        else:
            self._playing = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def speak(self, text: str, *, blocking: bool | None = None) -> None:
        if blocking is None:
            blocking = self._speak_blocking
        self._play_file(self._synth(text), blocking=blocking)

    async def _wait_quiet(self, timeout: float = 8.0) -> None:
        """Let any in-flight (non-blocking) speech finish before the next clip."""
        import time as _time

        t0 = _time.time()
        while self.speaking and _time.time() - t0 < timeout:
            await asyncio.sleep(0.08)

    # ── instant clips (GPT-Live-style acknowledgments) ────────────────────────
    # A wide, rotating repertoire: one canned "On it." repeated forever breaks
    # the illusion; dozens of task-neutral variations, never repeating recently,
    # read as a butler improvising — at ~50 ms, because they're all canned.
    # Acks are BUCKETED by request type (picked by a 0 ms keyword heuristic, so
    # instantness is preserved): queries get "Let me check.", actions get
    # "Consider it done.", destructive/outward requests get "Okay — carefully."
    _BANK_LINES = {
        "ack": (
            "On it.", "Sure.", "Sure thing.", "Absolutely.", "Of course.",
            "Happy to.", "Okay, on it.", "Give me a moment.", "One moment.",
            "Just a moment.", "Working on it.", "Getting to it.",
            "On the case.", "Alright, one sec.", "Already on it.",
            "Sure — one sec.", "Mm-hm, on it.",
        ),
        "ack_look": (
            "Let me check.", "Checking now.", "Let me take a look.",
            "Let's see.", "Looking into it now.", "Let me see what I can find.",
            "Taking a look.", "Okay, let me look.", "Let me dig in.",
            "Right, checking now.", "One moment — checking.", "Let me find out.",
        ),
        "ack_do": (
            "Consider it done.", "Right away.", "You got it.",
            "Coming right up.", "Say no more.", "Leave it to me.",
            "Let me handle that.", "Getting that for you.",
            "Alright, let's do it.", "Done and done — one sec.",
        ),
        "ack_careful": (
            "Okay — carefully.", "Sure — I'll be careful.",
            "On it — with care.", "Alright, double-checking as I go.",
            "Okay. I'll confirm before anything final.",
            "Careful mode — on it.",
        ),
        "hold": (
            "One sec — still with you.", "Still on it.", "Almost there.",
            "Bear with me.", "Just a bit longer.", "Nearly done.",
            "Still working — hang tight.", "This one's taking a moment.",
            "Still digging.", "Won't be long.", "Hang on, almost done.",
            "Give me a few more seconds.", "Still here, still working.",
            "Just wrapping up.",
        ),
        "stop": (
            "Stopping.", "Okay, stopping.", "Cancelled.", "Stopping now.",
            "Alright, dropping that.", "Done — stopped.",
        ),
    }

    # Request-type keywords for the 0 ms bucket pick. Careful wins over look
    # wins over do ("check the folder and delete it" deserves caution).
    _CAREFUL_WORDS = frozenset(
        "delete remove erase wipe drop kill uninstall overwrite destroy rm "
        "send email submit post publish push pay purchase buy deploy".split())
    _LOOK_WORDS = frozenset(
        "what when where who which why how check find search look show "
        "list count read tell is are does do did any status".split())
    _DO_WORDS = frozenset(
        "open create make move copy rename write run start launch install "
        "organize set change update download save build fix close".split())

    @classmethod
    def _ack_bucket(cls, text: str) -> str:
        words = {w.strip(".,!?;:'\"").lower() for w in (text or "").split()}
        if words & cls._CAREFUL_WORDS:
            return "ack_careful"
        if words & cls._LOOK_WORDS:
            return "ack_look"
        if words & cls._DO_WORDS:
            return "ack_do"
        return "ack"

    # Persistent per-voice clip cache: synthesize once, reuse across restarts.
    BANK_CACHE = Path.home() / ".neovis" / "cache" / "tts_bank"

    def _bank_path(self, kind: str, line: str) -> Path:
        import hashlib

        h = hashlib.md5(line.encode()).hexdigest()[:10]
        return self.BANK_CACHE / self.tts.voice_name / f"{kind}_{h}.wav"

    def build_ack_bank(self) -> None:
        """Make instant clips available NOW (from the disk cache, or by
        synthesizing one seed per kind), then fill the whole repertoire in the
        background. Rebuilt whenever the voice changes."""
        self._bank = {kind: [] for kind in self._BANK_LINES}
        (self.BANK_CACHE / self.tts.voice_name).mkdir(parents=True, exist_ok=True)
        for kind, lines in self._BANK_LINES.items():
            for line in lines:  # everything already cached loads instantly
                wav = self._bank_path(kind, line)
                if wav.exists():
                    self._bank[kind].append((line, wav))
        for kind, lines in self._BANK_LINES.items():
            if not self._bank[kind]:  # cold cache: seed one clip synchronously
                try:
                    wav = self._bank_path(kind, lines[0])
                    self.tts.synthesize(lines[0], wav)
                    self._bank[kind].append((lines[0], wav))
                except Exception:
                    pass
        try:  # synthesize the rest without blocking startup
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (tests / teardown) — seeds are enough
        loop.create_task(self._fill_bank())

    async def _fill_bank(self) -> None:
        """Background: synthesize missing clips one at a time on the loop
        thread (same thread as all other TTS — no races), yielding between."""
        for kind, lines in self._BANK_LINES.items():
            for line in lines:
                wav = self._bank_path(kind, line)
                if wav.exists():
                    continue
                try:
                    self.tts.synthesize(line, wav)
                    self._bank.setdefault(kind, []).append((line, wav))
                except Exception:
                    return
                await asyncio.sleep(0.2)  # stay out of the conversation's way

    def play_cached(self, kind: str) -> None:
        """Play a pre-synthesized clip instantly, avoiding recent repeats."""
        import random
        from collections import deque

        clips = self._bank.get(kind) or []
        if not clips and kind.startswith("ack_"):
            clips = self._bank.get("ack") or []  # bucket cold → generic acks
        if not clips:
            line = self._BANK_LINES.get(kind, ("Okay.",))[0]
            self.speak(line, blocking=False)
            return
        if not hasattr(self, "_recent_clips"):
            self._recent_clips = deque(maxlen=8)
        fresh = [c for c in clips if c[0] not in self._recent_clips] or clips
        line, wav = random.choice(fresh)
        self._recent_clips.append(line)
        self._last_spoken = f"{self._last_spoken} {line}"[-400:]  # echo reference
        if not self.speaking:
            self._play_file(wav, blocking=False)

    @property
    def speaking(self) -> bool:
        import time as _time

        alive = self._playing is not None and self._playing.poll() is None
        if not alive and self._playing is not None:
            self._playing = None
            self._speak_end = _time.time()
        return alive

    def stop_speaking(self) -> None:
        import time as _time

        if self._playing is not None and self._playing.poll() is None:
            self._playing.terminate()
        self._playing = None
        self._speak_end = _time.time()

    def _is_own_echo(self, text: str) -> bool:
        """True when a transcript is (a fragment of) what Neovis itself just
        said — its speaker leaking into the mic. Without echo cancellation this
        is the only thing standing between us and Neovis chatting with itself."""
        import time as _time
        from difflib import SequenceMatcher

        if not text or not self._last_spoken:
            return False
        if not self.speaking and (_time.time() - self._speak_end) > 2.0:
            return False  # too long after playback to be a leak
        a, b = text.lower().strip(" .,!?"), self._last_spoken.lower()
        if a in b:
            return True
        m = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
        return m.size >= 0.8 * len(a)

    async def handle_utterance(self, text: str, *, intent: dict | None = None) -> str:
        # The fast model (Haiku) decides what this means; the big model executes.
        # A caller that already classified (e.g. a steer) passes `intent` and
        # skips routing entirely.
        from ...core.router import _fast_rules, rule_fallback

        trace = _Trace(text, asr_s=self.last_asr_s)
        self._trace = trace
        self._narrated = ""
        self.ui.thinking(text)
        if intent is None:
            intent = _fast_rules(text) if self.router is not None else rule_fallback(text)
        send_task = None
        if intent is None:
            # Speculative execution: ≥3 words is almost certainly a task, so
            # start the agent turn NOW and let Haiku classify in parallel — its
            # 2-3 s no longer sits in front of every reply. 1-2 word utterances
            # ("hey", "thanks") are usually chat/stop: don't burn a main-model
            # turn on them, just wait for Haiku.
            words = text.split()
            if len(words) >= 3:
                # Ack real requests instantly — but "On it." after "thanks so
                # much" would be absurd, so social openers don't get one.
                social = words[0].lower().strip(",.!") in (
                    "thanks", "thank", "ok", "okay", "cool", "nice", "great",
                    "perfect", "awesome", "good", "hey", "hi", "hello")
                if len(words) >= 4 and not social:
                    self.play_cached(self._ack_bucket(text))  # ~50 ms, typed
                    trace.mark("ack")
                send_task = asyncio.ensure_future(self._run_task(text))
            intent = await self.router.classify(text)
            trace.mark("router")
        action = intent.get("action", "task")
        trace.d["action"] = action

        async def unwind():
            """Abandon a mis-speculated turn AFTER the user already got their
            answer — interrupting the engine can take seconds and must never
            sit in front of the reply."""
            if send_task is not None:
                self.session.stop()
                await asyncio.gather(send_task, return_exceptions=True)

        if action == "stop":
            self.ui.response("Stopping.", "")
            self.play_cached("stop")
            self.session.stop()
            await unwind()
            trace.done()
            return "(stopped)"
        if action == "voice":
            out = self._switch_voice(intent.get("name"), intent.get("accent"), intent.get("gender"))
            self.ui.idle()
            await unwind()
            trace.done()
            return out
        if action == "chat":
            # Pure small talk: Haiku already wrote the line — speak it now
            # (~2-3 s total) instead of a 10 s main-model turn on "hey".
            line = str(intent.get("reply") or "Hey — what can I do for you?")
            wav = self._synth(line)
            self.ui.response(line, "")          # text and audio land together
            trace.mark("spoken")
            self._play_file(wav, blocking=self._speak_blocking)
            await unwind()
            trace.done()
            return line
        # task → the (already running) gated agent turn
        if send_task is None:
            send_task = asyncio.ensure_future(self._run_task(text))
        reply = await send_task
        trace.mark("turn")
        spoken, detail = split_reply(reply)
        if self._narrated:
            # Narration IS the speech: the last streamed line is the answer
            # line; the panel shows the whole story (each stage + result).
            spoken, detail = self._narrated, reply.strip()
        if self._narrated and self._narrated_played:
            self.ui.response(spoken, detail)    # already heard it — just show
        else:
            await self._wait_quiet()            # let a narration clip finish
            wav = self._synth(spoken)
            self.ui.response(spoken, detail)    # text and audio land together
            trace.mark("spoken")
            self._play_file(wav, blocking=self._speak_blocking)
        trace.done(narrated=bool(self._narrated))
        return reply

    # ── never-blocked dispatch (GPT-Live: 'keeps the conversation going') ────
    def submit(self, text: str) -> None:
        """Hand a finished utterance to Neovis WITHOUT blocking the mic loop.
        Free → a normal turn. Busy → the utterance still gets an answer:
        Haiku chats/stops instantly, or the running task is steered."""
        print("you:", text)
        if self._turn_task is not None and not self._turn_task.done():
            asyncio.ensure_future(self._while_busy(text))
        else:
            self._turn_task = asyncio.ensure_future(self._dispatch(text))

    async def _dispatch(self, text: str, intent: dict | None = None) -> None:
        try:
            print("neovis>", await self.handle_utterance(text, intent=intent))
        except Exception as exc:
            print("neovis error:", exc)
            self.ui.error(str(exc)[:48])

    async def _while_busy(self, text: str) -> None:
        """A voice arrived while a task is running — the conversation must not
        go dead. stop → stop; chat → Haiku answers; anything else → steer the
        running task (interrupt-and-redirect, same as Slack)."""
        from ...core.router import _fast_rules, rule_fallback

        if self._busy_reply:
            return
        self._busy_reply = True
        try:
            intent = _fast_rules(text)
            if intent is None:
                intent = (await self.router.classify(text, busy=True)
                          if self.router is not None else rule_fallback(text))
            action = intent.get("action", "task")
            if action == "stop":
                self.ui.response("Stopping.", "")
                self.play_cached("stop")
                self.session.stop()
                await asyncio.gather(self._turn_task, return_exceptions=True)
                return
            if action == "chat":
                line = str(intent.get("reply") or "Still here — working on it.")
                await self._wait_quiet(3)
                self.ui.response(line, "")
                self.speak(line, blocking=False)
                return
            if action == "voice":
                self._switch_voice(intent.get("name"), intent.get("accent"), intent.get("gender"))
                return
            # a new/changed request → voice steer
            self.ui.thinking(f"redirect: {text[:44]}")
            self.speak("Okay, switching to that.", blocking=False)
            self.session.stop()
            old = self._turn_task
            self._turn_task = None
            await asyncio.gather(old, return_exceptions=True)
            # already classified as task — dispatch WITHOUT re-routing
            self._turn_task = asyncio.ensure_future(
                self._dispatch(_VOICE_STEER_NOTE + text, intent={"action": "task"}))
        finally:
            self._busy_reply = False

    def _narrate(self, block_text: str) -> None:
        """Speak the model's interim commentary as it streams ('Checking the
        repository now.') so working time is narrated, not silent."""
        line = _first_sentence(block_text)
        if not line:
            return
        self._narrated = line
        self._narrated_played = False
        if self._trace is not None and "first_voice_s" not in self._trace.d:
            self._trace.mark("first_voice")
        self.ui.step(line[:64])
        if self.speaking:
            return  # never overlap clips; the capsule still shows the line
        self._play_file(self._synth(line), blocking=False)
        self._narrated_played = True

    async def _run_task(self, text: str) -> str:
        # GPT-Live-style keep-alive: if the engine goes quiet for ~9 s with no
        # narration, say "one sec — still with you" so silence never reads as
        # a hang. At most twice per turn.
        import time as _time

        async def keepalive():
            for _ in range(2):
                await asyncio.sleep(9)
                if not self.speaking and _time.time() - self._speak_end > 6:
                    self.play_cached("hold")

        ka = asyncio.create_task(keepalive())
        try:
            return await self.session.send(
                text + VOICE_REPLY_NOTES,
                on_tool=lambda name, tool_input: self.ui.step(_format_step(name, tool_input)),
                on_text=self._narrate,
                transcript_text=text,  # recall stores the raw utterance, not the scaffolding
            )
        finally:
            ka.cancel()

    def _switch_voice(self, name: str | None, accent: str | None, gender: str | None) -> str:
        """Fill the missing dimension from the current voice and announce; if
        nothing usable was given, ask which voice."""
        _, cur_accent, cur_gender = (self.tts.voice_name,) + VOICES[self.tts.voice_name][1:]

        if name:
            self.tts.set_voice(name=name)
            self.build_ack_bank()
            return self._announce_voice(inferred=False)

        if not accent and not gender:
            self.speak(
                "Sure. I can do American or British, male or female. "
                f"You're on {cur_accent} {cur_gender} now. Which would you like?"
            )
            return "(asked which voice)"

        self.tts.set_voice(accent=accent or cur_accent, gender=gender or cur_gender)
        self.build_ack_bank()  # instant clips must speak with the new voice
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
                    import time as _time

                    self.ui.thinking("…")
                    _t0 = _time.time()
                    text = self.asr.transcribe_samples(pending_audio.pop(0), samplerate)
                    self.last_asr_s = _time.time() - _t0
                    if text.strip():
                        self.submit(text)  # non-blocking: press-and-talk anytime
                    else:
                        print("you: (nothing heard)")
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

        # threshold 0.3: catch soft first syllables (the default 0.5 swallowed
        # the user's first word); min_silence 0.7: a mid-sentence thinking
        # pause no longer chops the utterance. (A/B/C tested — prepending a
        # pre-roll buffer duplicates words, sherpa segments already pad onset.)
        vad = VAD(sample_rate=samplerate, threshold=0.3, min_silence=0.7)
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

        def purge():
            """Drop everything the mic collected that isn't fresh user speech:
            the VAD state, and any queued audio backlog (e.g. the 10 s of chunks
            that piled up while a turn was being handled)."""
            vad.reset()
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        pend = ""        # incomplete utterance held open for a continuation
        pend_at = 0.0
        try:
            while True:
                if pend and _time.time() > pend_at:  # continuation never came
                    self.submit(pend)
                    pend = ""
                try:
                    chunk = await asyncio.to_thread(q.get, True, 0.2)
                except queue.Empty:
                    continue
                frames = chunk.flatten()

                if self.speaking:
                    was_speaking = True
                    if not barge_in:
                        continue  # mic is muted while Neovis talks (speaker echo)
                elif was_speaking:
                    was_speaking = False
                    purge()  # discard anything captured around playback
                    quiet_until = _time.time() + 0.35  # echo tail
                    continue
                elif _time.time() < quiet_until:
                    continue

                vad.accept(frames)
                rms = float(np.sqrt((frames ** 2).mean()))
                self.ui.level(rms)
                # Barge-in needs BOTH voice activity and real loudness: a close
                # mic'd human is far louder than speaker leak, so this keeps
                # speaker users from having replies cut by their own echo.
                if barge_in and self.speaking and vad.is_speaking() and rms > 0.04:
                    self.stop_speaking()  # talk over Neovis to cut it off
                    purge()               # …but don't transcribe the trigger
                    quiet_until = _time.time() + 0.35
                    continue
                if vad.is_speaking() and not in_speech:
                    in_speech = True
                    self.ui.listening()
                for seg in vad.segments():
                    in_speech = False
                    _t0 = _time.time()
                    text = self.asr.transcribe_samples(seg, samplerate)
                    self.last_asr_s = _time.time() - _t0
                    if not text.strip():
                        continue
                    if self._is_own_echo(text):
                        print(f"(echo ignored: {text!r})")
                        continue
                    bare = text.lower().strip(" .,!?…")
                    if not pend and bare in _FILLERS:
                        continue  # a lone "um"/"oh" is thinking noise, not input
                    if pend:  # continuation of a held-open thought
                        text = f"{pend} {text}"
                        pend = ""
                    if not _utterance_complete(text):
                        pend, pend_at = text, _time.time() + 1.4
                        continue
                    # non-blocking: the mic stays live while the turn runs, so
                    # you can keep talking (status, steer, stop) mid-task.
                    self.submit(text)
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
        # A workstation JARVIS works from the user's home, not from whatever
        # repo the app was launched in — otherwise "what's going on?" turns
        # into an uninvited investigation of Neovis's own source tree.
        cwd=str(Path.home()),
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
    loop.build_ack_bank()  # instant acknowledgment clips, ready before turn 1

    async def cleanup():
        if loop._turn_task is not None:
            loop._turn_task.cancel()
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
