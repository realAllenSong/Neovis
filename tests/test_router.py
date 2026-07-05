"""The intent router's tier-0 rules and offline fallback (no model needed)."""

from neovis.core.router import _fast_rules, rule_fallback


def test_exact_stop_words_short_circuit():
    for w in ("stop", "cancel", "never mind", "cut it out"):
        assert _fast_rules(w) == {"action": "stop"}


def test_explicit_voice_short_circuits():
    r = _fast_rules("switch to a british male voice")
    assert r is not None and r["action"] == "voice"
    assert r["accent"] == "british" and r["gender"] == "male"

    r2 = _fast_rules("use the emma voice")
    assert r2["name"] == "emma"


def test_natural_phrasing_escalates_to_model():
    # The rules aren't sure — return None so classify() asks Haiku.
    assert _fast_rules("make it sound posh") is None
    assert _fast_rules("email the q2 numbers to my boss") is None


def test_offline_fallback_defaults_to_task():
    assert rule_fallback("email my boss") == {"action": "task"}
    assert rule_fallback("stop") == {"action": "stop"}
