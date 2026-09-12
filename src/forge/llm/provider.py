from __future__ import annotations

from agno.models.litellm import LiteLLM

from forge.config import ForgeConfig, Settings

# Defaults when forge.yml models are plain strings (no per-alias overrides)
_DEFAULT_TEMPERATURE = 0.1
_DEFAULT_MAX_TOKENS = 8192

# Task → model alias mapping
_TASK_ALIAS: dict[str, str] = {
    "review": "code",
    "chat": "default",
    "pipeline_debug": "fast",
    "security_triage": "strong",
}


def get_model(alias: str, config: ForgeConfig, settings: Settings) -> LiteLLM:
    """Create an Agno LiteLLM model from a forge.yml alias.

    Model entries in forge.yml can be either a plain string (model ID) or a dict
    with ``id``, ``temperature``, and ``max_tokens`` keys.
    """
    raw = config.models.get(alias, alias)

    if isinstance(raw, str):
        model_id = raw
        temperature = _DEFAULT_TEMPERATURE
        max_tokens = _DEFAULT_MAX_TOKENS
    else:
        model_id = raw["id"]
        temperature = raw.get("temperature", _DEFAULT_TEMPERATURE)
        max_tokens = raw.get("max_tokens", _DEFAULT_MAX_TOKENS)

    return LiteLLM(
        id=model_id,
        api_base=settings.LITELLM_URL,
        api_key="not-needed-for-proxy",
        temperature=temperature,
        max_tokens=max_tokens,
    )


def get_model_for_task(task: str, config: ForgeConfig, settings: Settings) -> LiteLLM:
    """Return the appropriate model for a given task type.

    Known tasks: review, chat, pipeline_debug, security_triage.
    Falls back to the "default" alias for unknown tasks.
    """
    alias = _TASK_ALIAS.get(task, "default")
    return get_model(alias, config, settings)
