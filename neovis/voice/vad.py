"""Voice activity detection (silero VAD via sherpa-onnx).

Used for hands-free / streaming mode: segment the mic stream into utterances
(so the user doesn't hold a key), and detect when the user starts speaking while
Neovis is talking — that's the barge-in signal to stop the reply and listen.
"""

from __future__ import annotations

from pathlib import Path

from . import MODELS_DIR

TEN_VAD_MODEL = MODELS_DIR / "ten-vad.onnx"
SILERO_VAD_MODEL = MODELS_DIR / "silero_vad.onnx"


class VAD:
    """Voice activity detector. Defaults to TEN VAD (newer, more accurate than
    silero on noisy/short speech); falls back to silero if TEN isn't present."""

    def __init__(
        self,
        engine: str | None = None,
        model_path: str | Path | None = None,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence: float = 0.4,
        min_speech: float = 0.2,
    ):
        import sherpa_onnx

        if engine is None:
            engine = "ten" if TEN_VAD_MODEL.exists() else "silero"
        self.engine = engine

        cfg = sherpa_onnx.VadModelConfig()
        sub = cfg.ten_vad if engine == "ten" else cfg.silero_vad
        sub.model = str(model_path or (TEN_VAD_MODEL if engine == "ten" else SILERO_VAD_MODEL))
        sub.threshold = threshold
        sub.min_silence_duration = min_silence
        sub.min_speech_duration = min_speech
        cfg.sample_rate = sample_rate
        self.sample_rate = sample_rate
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)

    def accept(self, samples) -> None:
        self._vad.accept_waveform(samples)

    def is_speaking(self) -> bool:
        return self._vad.is_speech_detected()

    def segments(self) -> list:
        """Return completed utterance segments (float32 arrays) and clear them."""
        out = []
        while not self._vad.empty():
            out.append(self._vad.front.samples)
            self._vad.pop()
        return out

    def flush(self) -> None:
        self._vad.flush()

    def reset(self) -> None:
        self._vad.reset()
