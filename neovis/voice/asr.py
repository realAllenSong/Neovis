"""Speech-to-text via sherpa-onnx (local, CPU) — two engines, one interface.

* **Parakeet-TDT-0.6b-v2 int8** (default) — transducer, English-only, RTF ~0.04
  on an M-series CPU. Hotword biasing via ``modified_beam_search``.
* **Qwen3-ASR-0.6B int8** — LLM-decoder, multilingual (Chinese + English +
  code-switching), RTF ~0.14 on the same CPU. Hotword biasing via its native
  context prompt. Pick with ``asr: qwen3`` in ``~/.neovis/settings.yaml``.

Both engines get the same phrases (the names Neovis remembers) and both are
followed by the engine-agnostic fuzzy correction pass (:mod:`.hotwords`).
Measured head-to-head (same audio, 2 threads):

    parakeet  rtf=0.04  'Alice Jang' → +hotwords 'Alice Zhang'
    qwen3     rtf=0.14  'Alice Jiang' → +hotwords 'Alice Zhang'
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from . import MODELS_DIR

# Preferred: NVIDIA Parakeet-TDT-0.6b-v2 int8 (top English accuracy, cased +
# punctuated, CPU real-time via sherpa-onnx). Fallback: the small zipformer,
# which ships bpe.model so hotwords (contextual biasing) work.
PARAKEET_DIR = MODELS_DIR / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8"
ZIPFORMER_DIR = MODELS_DIR / "sherpa-onnx-zipformer-en-2023-04-01"
QWEN3_DIR = MODELS_DIR / "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"
DEFAULT_ASR_DIR = PARAKEET_DIR if PARAKEET_DIR.exists() else ZIPFORMER_DIR


class _Transcriber:
    """Shared decode + hotword-correction pass over a sherpa-onnx recognizer."""

    _rec = None
    phrases: list[str] = []

    def transcribe(self, wav_path: str | Path) -> str:
        import soundfile as sf

        samples, sr = sf.read(str(wav_path), dtype="float32")
        if getattr(samples, "ndim", 1) > 1:
            samples = samples[:, 0]
        return self.transcribe_samples(samples, sr)

    def transcribe_samples(self, samples, sample_rate: int) -> str:
        """Transcribe raw float32 mono samples (for live mic capture)."""
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._rec.decode_stream(stream)
        text = stream.result.text.strip()
        if text and self.phrases:
            from .hotwords import correct_transcript

            text = correct_transcript(text, self.phrases)
        return text


def _export_bpe_vocab(model_path: Path, out_path: Path) -> None:
    """sherpa-onnx wants a text 'token score' vocab, not the binary bpe.model."""
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(model_path))
    with open(out_path, "w") as f:
        for i in range(sp.get_piece_size()):
            f.write(f"{sp.id_to_piece(i)} {sp.get_score(i)}\n")


def _ensure_bpe_vocab(model_dir: Path) -> Path | None:
    """The bpe vocab used to tokenize hotword phrases, best source first:
    an existing bpe.vocab → exported from bpe.model → synthesized from
    tokens.txt for NeMo models (Parakeet ships no SentencePiece model; a
    uniform-score vocab over its pieces is enough for hotword encoding —
    A/B verified: 'Jiyuan Song'→'Zhiyuan Song', 'Alice Jang'→'Alice Zhang')."""
    vocab = model_dir / "bpe.vocab"
    if vocab.exists():
        return vocab
    bpe_model = model_dir / "bpe.model"
    if bpe_model.exists():
        _export_bpe_vocab(bpe_model, vocab)
        return vocab
    if "nemo" in model_dir.name or "parakeet" in model_dir.name:
        lines = []
        for ln in (model_dir / "tokens.txt").read_text(encoding="utf-8").splitlines():
            parts = ln.rsplit(" ", 1)
            if len(parts) == 2:
                lines.append(f"{parts[0]} 0.0")
        vocab.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return vocab
    return None


class TransducerASR(_Transcriber):
    def __init__(
        self,
        model_dir: str | Path = DEFAULT_ASR_DIR,
        *,
        hotwords: list[str] | None = None,
        hotwords_score: float = 2.0,
        num_threads: int = 2,
        int8: bool = True,
    ):
        import sherpa_onnx

        d = Path(model_dir)
        suffix = ".int8" if int8 else ""

        def one(prefix: str) -> str:
            # zipformer names: encoder-epoch-…​.int8.onnx; parakeet: encoder.int8.onnx
            hits = sorted(d.glob(f"{prefix}*{suffix}.onnx"))
            if not hits:
                hits = sorted(d.glob(f"{prefix}*.onnx"))
            if not hits:
                raise FileNotFoundError(f"no {prefix} onnx in {d}")
            return str(hits[0])

        kwargs = dict(
            encoder=one("encoder"),
            decoder=one("decoder"),
            joiner=one("joiner"),
            tokens=str(d / "tokens.txt"),
            num_threads=num_threads,
            decoding_method="greedy_search",
        )
        if "nemo" in d.name or "parakeet" in d.name:
            kwargs["model_type"] = "nemo_transducer"

        # Two hotword layers, both fed the same phrases (e.g. names Neovis
        # remembers): native beam-search biasing when a bpe vocab is available,
        # and a fuzzy post-correction pass that always runs (asr → memory
        # spelling, engine-agnostic).
        self.phrases: list[str] = [p for p in (hotwords or []) if p.strip()]
        self.hotwords_active = False
        bpe_vocab = _ensure_bpe_vocab(d) if self.phrases else None
        if self.phrases and bpe_vocab is not None:
            hw_file = Path(tempfile.gettempdir()) / "neovis_hotwords.txt"
            hw_file.write_text("\n".join(self.phrases) + "\n")
            kwargs.update(
                decoding_method="modified_beam_search",
                hotwords_file=str(hw_file),
                hotwords_score=hotwords_score,
                modeling_unit="bpe",
                bpe_vocab=str(bpe_vocab),
            )
            self.hotwords_active = True

        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(**kwargs)


class Qwen3ASR(_Transcriber):
    """Qwen3-ASR-0.6B int8 — multilingual (Chinese/English/code-switching),
    hotwords via the model's native context prompt. ~3-4x slower than Parakeet
    on CPU but still well under real time."""

    def __init__(
        self,
        model_dir: str | Path = QWEN3_DIR,
        *,
        hotwords: list[str] | None = None,
        num_threads: int = 2,
    ):
        import sherpa_onnx

        d = Path(model_dir)
        self.phrases = [p for p in (hotwords or []) if p.strip()]
        self.hotwords_active = bool(self.phrases)
        self._rec = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=str(d / "conv_frontend.onnx"),
            encoder=str(d / "encoder.int8.onnx"),
            decoder=str(d / "decoder.int8.onnx"),
            tokenizer=str(d / "tokenizer"),
            num_threads=num_threads,
            hotwords=", ".join(self.phrases),
        )


def _select_engine(engine: str, parakeet: bool, qwen3: bool) -> str:
    """Pure selection logic: an explicit, installed choice wins; 'auto' prefers
    parakeet (fastest), then qwen3, then the zipformer fallback."""
    if engine == "qwen3" and qwen3:
        return "qwen3"
    if engine == "parakeet" and parakeet:
        return "parakeet"
    if parakeet:
        return "parakeet"
    if qwen3:
        return "qwen3"
    return "zipformer"


def build_asr(*, engine: str | None = None, hotwords: list[str] | None = None):
    """The configured ASR engine (settings key ``asr``: auto|parakeet|qwen3)."""
    if engine is None:
        from ..core.settings import load

        engine = str(load().get("asr") or "auto")
    choice = _select_engine(engine, PARAKEET_DIR.exists(), QWEN3_DIR.exists())
    if choice == "qwen3":
        return Qwen3ASR(hotwords=hotwords)
    if choice == "parakeet":
        return TransducerASR(model_dir=PARAKEET_DIR, hotwords=hotwords)
    return TransducerASR(model_dir=ZIPFORMER_DIR, hotwords=hotwords)
