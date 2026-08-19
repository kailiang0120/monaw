"""Shared model identifiers for LLM settings and runtime wiring."""

DEFAULT_OPENAI_CHAT_MODEL = "gpt-5.6-luna"
OPENAI_CHAT_MODELS = (DEFAULT_OPENAI_CHAT_MODEL,)

DEFAULT_GEMINI_CHAT_MODEL = "gemini-3.1-pro-preview"
GEMINI_CHAT_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
)

CHAT_MODELS_BY_PROVIDER = {
    "openai": OPENAI_CHAT_MODELS,
    "gemini": GEMINI_CHAT_MODELS,
}
