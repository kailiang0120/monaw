"""Shared model identifiers for LLM settings and runtime wiring."""

DEFAULT_OPENAI_CHAT_MODEL = "gpt-6-luna"
OPENAI_CHAT_MODELS = (
    "gpt-6-luna",
    "gpt-6.1-sol",
    "gpt-6-astra",
)

DEFAULT_GEMINI_CHAT_MODEL = "gemini-pro-latest"
GEMINI_CHAT_MODELS = (
    "gemini-pro-latest",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
)

CHAT_MODELS_BY_PROVIDER = {
    "openai": OPENAI_CHAT_MODELS,
    "codex": OPENAI_CHAT_MODELS,
    "gemini": GEMINI_CHAT_MODELS,
}


def normalize_chat_model(provider: str, model_name: str) -> str:
    """Migrate saved selections within their existing model family."""
    if provider in {"openai", "codex"}:
        model = "gpt-6.1-sol" if model_name == "gpt-6-sol" else model_name
        return model if model in OPENAI_CHAT_MODELS else DEFAULT_OPENAI_CHAT_MODEL
    if provider == "gemini":
        migrations = {
            "gemini-2.5-pro": "gemini-pro-latest",
            "gemini-3-pro-preview": "gemini-pro-latest",
            "gemini-3.1-pro-preview": "gemini-pro-latest",
            "gemini-2.5-flash": "gemini-flash-latest",
            "gemini-3-flash-preview": "gemini-flash-latest",
            "gemini-3.5-flash": "gemini-flash-latest",
            "gemini-3.6-flash": "gemini-flash-latest",
            "gemini-3.7-flash": "gemini-flash-latest",
            "gemini-3.8-flash": "gemini-flash-latest",
            "gemini-2.5-flash-lite": "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite": "gemini-flash-lite-latest",
            "gemini-3.1-flash-lite-preview": "gemini-flash-lite-latest",
            "gemini-3.5-flash-lite": "gemini-flash-lite-latest",
        }
        model = migrations.get(model_name, model_name)
        return model if model in GEMINI_CHAT_MODELS else DEFAULT_GEMINI_CHAT_MODEL
    return DEFAULT_OPENAI_CHAT_MODEL
