"""Configuration loading.

Everything a deploy needs to change lives in two YAML files:

* ``config/models.yaml``  — LLM/ASR/TTS endpoints. Point ``base_url`` at your
  company proxy and set the provider; the agent core never hardcodes a model.
* ``config/policy.yaml``  — the security posture (see :mod:`.policy`).

API keys are never written to YAML. Files reference an environment variable by
name (``api_key_env``) and ``${VAR}`` placeholders are expanded at load time.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from .policy import PolicyConfig

_ENV_RE = re.compile(r"\$\{([^}]+)\}")

# Repo root -> config/ ships defaults; a deploy can override via NEOVIS_CONFIG_DIR.
_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders in strings."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


class ModelConfig(BaseModel):
    """A single generative endpoint (LLM). Model-agnostic by construction."""

    provider: Literal["openai", "anthropic"]
    model: str
    base_url: str | None = None          # your proxy; None => provider default
    api_key_env: str = "NEOVIS_API_KEY"  # env var holding the key


class VoiceConfig(BaseModel):
    """ASR/TTS endpoints. Optional — text works without these."""

    asr_provider: Literal["openai", "faster-whisper", "none"] = "none"
    asr_model: str = "whisper-1"
    tts_provider: Literal["openai", "edge-tts", "none"] = "none"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    base_url: str | None = None
    api_key_env: str = "NEOVIS_API_KEY"


class AppConfig(BaseModel):
    """Everything, assembled."""

    llm: ModelConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    # Human-readable name for this machine, used when routing/announcing.
    host_label: str = Field(default_factory=lambda: os.uname().nodename if hasattr(os, "uname") else "workstation")


def config_dir() -> Path:
    override = os.environ.get("NEOVIS_CONFIG_DIR")
    return Path(override).expanduser() if override else _DEFAULT_CONFIG_DIR


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _expand_env(data)


def load_config() -> AppConfig:
    """Load ``models.yaml`` + ``policy.yaml`` from the active config dir."""
    cdir = config_dir()
    models = _load_yaml(cdir / "models.yaml")
    policy = _load_yaml(cdir / "policy.yaml")

    if "llm" not in models:
        raise ValueError(
            f"models.yaml at {cdir} must define an 'llm' section "
            "(provider, model, base_url, api_key_env)."
        )

    return AppConfig(
        llm=ModelConfig(**models["llm"]),
        voice=VoiceConfig(**models.get("voice", {})),
        policy=PolicyConfig(**policy),
    )
