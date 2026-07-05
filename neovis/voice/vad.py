"""Voice activity detection (silero VAD via sherpa-onnx).

Used for hands-free / streaming mode: segment the mic stream into utterances
(so the user doesn't hold a key), and detect when the user starts speaking while
Neovis is talking — that's the barge-in signal to stop the reply and listen.
"""

from __future__ import annotations

from pathlib import Path

from . import MODELS_DIR

DEFAULT_VAD_MODEL = MODELS_DIR / "silero_vad.onnx"


class VAD:
    def __init__(
        self,
        model_path: str | Path = DEFAULT_VAD_MODEL,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence: float = 0.4,
        min_speech: float = 0.2,
    ):
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
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
