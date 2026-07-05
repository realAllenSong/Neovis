"""Parse spoken control commands that Neovis handles itself (not the LLM).

Currently: changing the assistant's voice. Users won't phrase it precisely, so we
parse *whatever dimensions they gave* (a name, and/or an accent, and/or a
gender) into a :class:`VoiceIntent`. The caller fills any missing dimension from
the current voice and always announces the result — so "make it British" keeps
the current gender and confirms, while a bare "change your voice" asks which one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .tts import VOICES

_CHANGE_WORDS = ("voice", "accent", "speak", "sound", "talk", "switch", "change")
_BRITISH = ("british", "uk", "england", "english accent", "brit")
_AMERICAN = ("american", "us accent", "u.s", "yank")
# check female before male — "female" contains the substring "male".
_MALE = (" male", " man", " guy", " gentleman", " him ", " his ")
_FEMALE = ("female", "woman", "lady", " her ", " she ")


@dataclass
class VoiceIntent:
    name: str | None = None
    accent: str | None = None
    gender: str | None = None

    @property
    def specified(self) -> bool:
        return bool(self.name or self.accent or self.gender)


def parse_voice_command(text: str) -> VoiceIntent | None:
    """Return a VoiceIntent if the utterance is about the voice, else None.

    A returned intent may be empty (a change was requested but no accent/gender/
    name given) — the caller should then ask which voice.
    """
    t = f" {text.lower().strip()} "

    for name in VOICES:
        if any(f" {name}{sep}" in t for sep in (" ", ".", ",", "?", "!")):
            return VoiceIntent(name=name)

    accent = "british" if any(w in t for w in _BRITISH) else (
        "american" if any(w in t for w in _AMERICAN) else None
    )
    gender = "female" if any(w in t for w in _FEMALE) else (
        "male" if any(w in t for w in _MALE) else None
    )
    # It's a voice command if the user used a change word, or referred to the
    # assistant ("make it British", "you sound…") alongside an accent/gender.
    referent = any(r in t for r in (" it ", " you ", " your ", " yourself "))
    if any(w in t for w in _CHANGE_WORDS) or ((accent or gender) and referent):
        return VoiceIntent(accent=accent, gender=gender)
    return None
