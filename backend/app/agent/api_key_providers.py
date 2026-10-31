"""Choosing between the user's OpenAI and Google API keys for a feature."""

from __future__ import annotations

KEY_PROVIDERS = ("openai", "gemini")


def saved_api_keys(base_settings) -> dict[str, str]:
    return {
        "openai": str(getattr(base_settings, "openai_api_key", "") or "").strip(),
        "gemini": str(getattr(base_settings, "google_api_key", "") or "").strip(),
    }


def resolve_key_provider(preference: str, keys: dict[str, str], chat_provider: str = "") -> str:
    """Return the provider a feature uses, or "" when it has no key.

    "auto" follows the chat model's provider when its key is saved, then
    prefers OpenAI, then Google. A ChatGPT sign-in ("codex") has no API key.
    """
    if preference in KEY_PROVIDERS:
        return preference if keys.get(preference) else ""
    if chat_provider in KEY_PROVIDERS and keys.get(chat_provider):
        return chat_provider
    return next((provider for provider in KEY_PROVIDERS if keys.get(provider)), "")
