"""Text-to-speech via Kokoro-82M on sherpa-onnx (local, CPU, English).

Kokoro is Apache-2.0, ~5x+ realtime on CPU, and topped the TTS Arena — a good
default for an offline English assistant voice. Only the model path is
configurable; everything runs on-device.
"""

from __future__ import annotations

from pathlib import Path

from . import MODELS_DIR

DEFAULT_KOKORO_DIR = MODELS_DIR / "kokoro-en-v0_19"


class KokoroTTS:
    def __init__(
        self,
        model_dir: str | Path = DEFAULT_KOKORO_DIR,
        *,
        num_threads: int = 2,
        speed: float = 1.0,
        sid: int = 0,
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
        self.sid = sid

    @property
    def sample_rate(self) -> int:
        return self._tts.sample_rate

    def synthesize(self, text: str, out_path: str | Path) -> tuple[str, float]:
        """Render ``text`` to a WAV file. Returns (path, duration_seconds)."""
        import soundfile as sf

        audio = self._tts.generate(text, sid=self.sid, speed=self.speed)
        sf.write(str(out_path), audio.samples, audio.sample_rate)
        duration = len(audio.samples) / audio.sample_rate
        return str(out_path), duration
