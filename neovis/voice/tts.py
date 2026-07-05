"""Text-to-speech via Kokoro-82M on sherpa-onnx (local, CPU, English).

Kokoro is Apache-2.0, ~5x+ realtime on CPU, and topped the TTS Arena — a good
default for an offline English assistant voice. Only the model path is
configurable; everything runs on-device.
"""

from __future__ import annotations

from pathlib import Path

from . import MODELS_DIR

DEFAULT_KOKORO_DIR = MODELS_DIR / "kokoro-en-v0_19"

# Named Kokoro voices (sherpa-onnx kokoro-en-v0_19 speaker ids).
# name -> (sid, accent, gender)
VOICES: dict[str, tuple[int, str, str]] = {
    "sky": (4, "american", "female"),
    "adam": (5, "american", "male"),
    "emma": (7, "british", "female"),
    "george": (9, "british", "male"),
}
DEFAULT_VOICE = "sky"


def resolve_voice(
    *, name: str | None = None, accent: str | None = None, gender: str | None = None
) -> tuple[int, str] | None:
    """Resolve a voice by name, or by (accent, gender). Returns (sid, name)."""
    if name and name.lower() in VOICES:
        return VOICES[name.lower()][0], name.lower()
    if accent or gender:
        acc = "british" if accent and any(
            w in accent.lower() for w in ("brit", "uk", "england", "english")
        ) else "american"
        gen = "male" if gender and gender.lower().startswith("m") else "female"
        for nm, (sid, a, g) in VOICES.items():
            if a == acc and g == gen:
                return sid, nm
    return None


class KokoroTTS:
    def __init__(
        self,
        model_dir: str | Path = DEFAULT_KOKORO_DIR,
        *,
        num_threads: int = 2,
        speed: float = 1.0,
        voice: str = DEFAULT_VOICE,
    ):
        import sherpa_onnx

        d = Path(model_dir)
        for f in ("model.onnx", "voices.bin", "tokens.txt"):
            if not (d / f).exists():
                raise FileNotFoundError(
                    f"Kokoro model file {f} missing in {d}. Download the model "
                    "(see README) into ~/.neovis/models."
                )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(d / "model.onnx"),
                    voices=str(d / "voices.bin"),
                    tokens=str(d / "tokens.txt"),
                    data_dir=str(d / "espeak-ng-data"),
                ),
                num_threads=num_threads,
                provider="cpu",
            ),
            max_num_sentences=2,
        )
        self._tts = sherpa_onnx.OfflineTts(config)
        self.speed = speed
        self.voice_name = voice if voice in VOICES else DEFAULT_VOICE
        self.sid = VOICES[self.voice_name][0]

    @property
    def sample_rate(self) -> int:
        return self._tts.sample_rate

    def set_voice(self, *, name: str | None = None, accent: str | None = None, gender: str | None = None) -> str | None:
        """Switch voice by name or (accent, gender). Returns the new voice name, or None."""
        resolved = resolve_voice(name=name, accent=accent, gender=gender)
        if resolved is None:
            return None
        self.sid, self.voice_name = resolved
        return self.voice_name

    def synthesize(self, text: str, out_path: str | Path) -> tuple[str, float]:
        """Render ``text`` to a WAV file. Returns (path, duration_seconds)."""
        import soundfile as sf

        audio = self._tts.generate(text, sid=self.sid, speed=self.speed)
        sf.write(str(out_path), audio.samples, audio.sample_rate)
        duration = len(audio.samples) / audio.sample_rate
        return str(out_path), duration
