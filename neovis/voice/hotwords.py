"""Memory → ASR hotwords: the names Neovis knows shape what it hears.

Two layers, both fed from persistent memory (MEMORY.md / USER.md):

1. **Biasing** — name-like phrases are handed to sherpa-onnx as hotwords when
   the loaded model supports contextual biasing (transducer + beam search +
   bpe vocab).
2. **Correction** — after transcription, name-like n-grams in the transcript
   are fuzzy-matched against the known phrases and snapped to the remembered
   spelling ("alice chang" → "Alice Zhang"). Pure difflib, engine-agnostic,
   so it works even where native biasing doesn't.

So the day after you tell Neovis "our CTO is Alice Zhang", saying her name
aloud just works — memory and voice are one story.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..core.memory import MemoryStore

# Words that look like names but aren't (roles, headers, common sentence-heads).
_STOP = {
    "the", "our", "my", "a", "an", "is", "are", "was", "at", "in", "on", "to",
    "cto", "ceo", "cfo", "coo", "hr", "it", "api", "url", "memory", "user",
    "daily", "weekly", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
}

# "Alice Zhang", "Alice", "Jean-Pierre" … 1-3 capitalized words.
_NAME_RX = re.compile(r"\b([A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?: [A-Z][a-z]+(?:[-'][A-Z][a-z]+)?){0,2})\b")
# Tickers / acronyms: 2-5 uppercase letters ("NVDA") — valuable at a fund.
_TICKER_RX = re.compile(r"\b[A-Z]{2,5}\b")


def names_from_memory(store: MemoryStore | None = None) -> list[str]:
    """Name-like phrases and tickers found in the memory stores, deduped,
    longest first (so multi-word names win over their fragments)."""
    store = store or MemoryStore()
    text = "\n".join(store.entries("memory") + store.entries("user"))
    found: dict[str, None] = {}
    for m in _NAME_RX.finditer(text):
        phrase = m.group(1)
        words = phrase.split()
        if all(w.lower() in _STOP for w in words):
            continue
        # drop a leading stop-word ("Our Alice Zhang" → "Alice Zhang")
        while words and words[0].lower() in _STOP:
            words = words[1:]
        if words:
            found.setdefault(" ".join(words))
    for m in _TICKER_RX.finditer(text):
        if m.group(0).lower() not in _STOP:
            found.setdefault(m.group(0))
    return sorted(found, key=lambda p: (-len(p.split()), -len(p)))


def correct_transcript(text: str, phrases: list[str], threshold: float = 0.8) -> str:
    """Snap near-miss n-grams in ``text`` to their remembered spelling.

    For each known phrase (longest first), slide a window of the same word
    count over the transcript; a window whose lowercase similarity ≥ threshold
    is replaced by the canonical phrase. Exact matches (case aside) also snap,
    so 'alice zhang' gains its proper capitalization.
    """
    if not text or not phrases:
        return text
    words = text.split()
    for phrase in phrases:
        target = phrase.lower()
        n = len(phrase.split())
        if n == 0 or n > len(words):
            continue
        i = 0
        while i <= len(words) - n:
            window = " ".join(words[i:i + n])
            bare = window.strip(".,!?;:").lower()
            # no first-letter guard: ASR mangles onsets too ("Zhiyuan"→"Jiyuan")
            if len(bare) >= 4:
                score = SequenceMatcher(None, bare, target).ratio()
                if score >= threshold:
                    tail = window[len(window.rstrip(".,!?;:")):]  # keep punctuation
                    words[i:i + n] = (phrase + tail).split()
                    i += len(phrase.split())
                    continue
            i += 1
    return " ".join(words)
