"""Voice I/O for the desktop entry point — all local (CPU), English.

Only the LLM "brain" is remote; speech-to-text and text-to-speech run on the
machine via sherpa-onnx (ONNX runtime):

* :mod:`.tts` — Kokoro-82M text-to-speech.
* :mod:`.asr` — a transducer ASR model with hotword (contextual-biasing)
  support, so fund-specific proper nouns (tickers, people's names) transcribe
  correctly.

Models live under ``~/.neovis/models`` by default.
"""

from pathlib import Path

MODELS_DIR = Path.home() / ".neovis" / "models"
