"""Speech-to-text via a sherpa-onnx transducer (local, CPU, English).

A transducer model is chosen specifically because it supports **hotwords**
(contextual biasing): fund-specific proper nouns — tickers, colleagues' names —
otherwise get transcribed as phonetic neighbours. Hotwords need the model's
``bpe.model`` (to tokenise the phrases) and ``modified_beam_search``; without it
we fall back to plain decoding.
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
DEFAULT_ASR_DIR = PARAKEET_DIR if PARAKEET_DIR.exists() else ZIPFORMER_DIR


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


class TransducerASR:
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
