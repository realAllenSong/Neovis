"""Parse spoken control commands that Neovis handles itself (not the LLM).

Currently: changing the assistant's voice — "switch to a British male accent",
"talk like an American woman", "use the george voice". An easter egg, but a nice
one for a voice assistant.
"""

from __future__ import annotations

from .tts import VOICES, resolve_voice

_CHANGE_WORDS = ("voice", "accent", "speak", "sound", "talk", "switch", "change")
_BRITISH = ("british", "uk", "england", "english accent", "brit")
_AMERICAN = ("american", "us accent", "u.s", "yank")
# NB: check female first — "female" contains the substring "male".
_MALE = (" male", " man", " guy", " gentleman", " him ", " his ")
_FEMALE = ("female", "woman", "lady", " her ", " she ")


def parse_voice_command(text: str) -> tuple[int, str] | None:
    """If the utterance asks to change the voice, return (sid, name); else None."""
    t = f" {text.lower().strip()} "

    # Named voice, e.g. "use the emma voice".
    for name in VOICES:
        if f" {name} " in t or f" {name}." in t:
            return resolve_voice(name=name)

    # Otherwise require a change-intent word plus an accent and/or gender.
    if not any(w in t for w in _CHANGE_WORDS):
        return None
    accent = "british" if any(w in t for w in _BRITISH) else (
        "american" if any(w in t for w in _AMERICAN) else None
    )
    gender = "female" if any(w in t for w in _FEMALE) else (
        "male" if any(w in t for w in _MALE) else None
    )
    if accent or gender:
        return resolve_voice(accent=accent, gender=gender)
    return None
