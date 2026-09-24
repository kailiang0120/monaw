"""Hardcoded model catalog for the settings UI."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.llm_constants import CHAT_MODELS_BY_PROVIDER


@dataclass(frozen=True)
class ProviderModelOptions:
    id: str
    label: str
    models: list[str]


def _provider_label(provider: str) -> str:
    if provider == "gemini":
        return "Google"
    if provider == "codex":
        return "OpenAI account"
    return "OpenAI API"


def model_options_payload() -> dict:
    providers = [
        ProviderModelOptions(
            id=provider,
            label=_provider_label(provider),
            models=list(models),
        ).__dict__
        for provider, models in CHAT_MODELS_BY_PROVIDER.items()
    ]
    return {"providers": providers}
