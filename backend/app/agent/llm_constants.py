"""Shared model identifiers for LLM settings and runtime wiring."""

from app.agent.model_registry import CONNECTIONS, GEMINI_MODELS, OPENAI_MODELS

DEFAULT_OPENAI_CHAT_MODEL = OPENAI_MODELS[0]
OPENAI_CHAT_MODELS = OPENAI_MODELS

DEFAULT_GEMINI_CHAT_MODEL = GEMINI_MODELS[0]
GEMINI_CHAT_MODELS = GEMINI_MODELS

CHAT_MODELS_BY_PROVIDER = {key: connection.models for key, connection in CONNECTIONS.items()}


MODEL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$"

# Model tokens one chat message may use before the task pauses for the user.
DEFAULT_MAX_TOKENS_PER_TURN = 5_000_000


def normalize_chat_model(provider: str, model_name: str, custom_models: tuple[str, ...] | list[str] = ()) -> str:
    """Migrate saved selections within their model family. A name the user added
    as a custom model is kept, and so is any name for a ChatGPT account, whose
    list comes from the account and changes over time."""
    model_name = str(model_name or "").strip()
    if model_name and model_name in custom_models:
        return model_name
    if provider == "codex":
        return model_name or DEFAULT_OPENAI_CHAT_MODEL
    if provider == "openai":
        # The API retired gpt-6-sol; a ChatGPT account still lists it.
        model = "gpt-6.1-sol" if model_name == "gpt-6-sol" else model_name
        return model or DEFAULT_OPENAI_CHAT_MODEL
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
        return model or DEFAULT_GEMINI_CHAT_MODEL
    return DEFAULT_OPENAI_CHAT_MODEL
