"""Model-agnostic LLM construction.

The agent core asks for a model by config, never by name. Point ``base_url`` at
your company proxy and switch ``provider`` between openai/anthropic with a
one-line YAML edit — the rest of the system is unchanged. Import names differ
across pydantic-ai versions, so we degrade gracefully.
"""

from __future__ import annotations

import os

from .config import ModelConfig


def build_model(cfg: ModelConfig):
    """Return a pydantic-ai model instance for ``cfg``."""
    api_key = os.environ.get(cfg.api_key_env, "")

    if cfg.provider == "openai":
        try:
            from pydantic_ai.models.openai import OpenAIChatModel as _OpenAIModel
        except ImportError:  # older pydantic-ai
            from pydantic_ai.models.openai import OpenAIModel as _OpenAIModel
        from pydantic_ai.providers.openai import OpenAIProvider

        provider_kwargs: dict = {"api_key": api_key}
        if cfg.base_url:
            provider_kwargs["base_url"] = cfg.base_url
        return _OpenAIModel(cfg.model, provider=OpenAIProvider(**provider_kwargs))

    if cfg.provider == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider_kwargs = {"api_key": api_key}
        if cfg.base_url:
            provider_kwargs["base_url"] = cfg.base_url
        return AnthropicModel(cfg.model, provider=AnthropicProvider(**provider_kwargs))

    raise ValueError(f"unsupported provider: {cfg.provider}")
