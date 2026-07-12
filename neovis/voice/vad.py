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
    """Voice activity detector.

    Defaults to **silero**, not TEN, on measured evidence: under sherpa-onnx,
    TEN VAD detects speech fine (``is_speech_detected`` works) but is far too
    eager to keep a segment OPEN — at threshold 0.3 it flagged 85% of a clip
    that was 45% silence, so segments only closed 1 time in 3, and when they
    did it took 2.4 s after the speaker stopped. Raising TEN's threshold to
    close them reliably starts swallowing first words. Silero at 0.3 closed
    every segment 0.41–0.45 s after speech ended with the first word intact.
    TEN remains available explicitly (``engine="ten"``) for noisy rooms, where
    its detection quality is the reason we adopted it.
    """

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
            engine = "silero" if SILERO_VAD_MODEL.exists() else "ten"
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
