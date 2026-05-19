"""Shared model identifiers for LLM settings and runtime wiring."""

OPENAI_CHAT_MODELS = (
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
)

DEEPSEEK_CHAT_MODELS = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)

DEFAULT_GEMINI_CHAT_MODEL = "gemini-3.1-pro-preview"
GEMINI_CHAT_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
)

CHAT_MODELS_BY_PROVIDER = {
    "openai": OPENAI_CHAT_MODELS,
    "deepseek": DEEPSEEK_CHAT_MODELS,
    "gemini": GEMINI_CHAT_MODELS,
}

DEFAULT_VISION_FALLBACK_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_VISION_FALLBACK_MAX_OUTPUT_TOKENS = 1000
VISION_FALLBACK_MODELS = (
    DEFAULT_VISION_FALLBACK_MODEL,
    "gemini-3.1-pro-preview",
)
