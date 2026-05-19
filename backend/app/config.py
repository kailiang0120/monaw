from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.llm_constants import DEFAULT_VISION_FALLBACK_MODEL


def _detect_env_file_encoding(env_file: str = ".env") -> str:
    path = Path(env_file)
    if not path.exists():
        return "utf-8"
    try:
        prefix = path.read_bytes()[:4]
    except OSError:
        return "utf-8"
    if prefix.startswith(b"\xff\xfe") or prefix.startswith(b"\xfe\xff"):
        return "utf-16"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    return "utf-8"


class Settings(BaseSettings):
    model_provider: str = "openai"
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    google_api_key: str = ""
    tavily_api_key: str = ""
    model_name: str = "gpt-5.4"
    reasoning_effort: str = "medium"
    vision_fallback_enabled: bool = True
    vision_fallback_model: str = DEFAULT_VISION_FALLBACK_MODEL
    vision_fallback_max_output_tokens: int = 1000
    port: int = 8435
    cors_allow_origins: str = "file://,null"
    cors_allow_origin_regex: str = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    allow_arbitrary_app_paths: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_allowed_chat_ids: str = ""
    telegram_allow_all: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding=_detect_env_file_encoding(".env"),
        extra="ignore",
        protected_namespaces=(),
    )


settings = Settings()
