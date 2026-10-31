"""Speech-to-text through the user's OpenAI or Google API key."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.agent.api_key_providers import resolve_key_provider, saved_api_keys

logger = logging.getLogger(__name__)

OPENAI_STT_MODEL = "gpt-4o-mini-transcribe"
GEMINI_STT_MODEL = "gemini-2.5-flash"
STT_REQUEST_TIMEOUT_SECONDS = 120
MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
VOICE_INPUT_NOT_CONFIGURED_MESSAGE = (
    "Voice input needs an OpenAI or Google API key. Add one in Settings > Connections."
)

_AUDIO_MIME_TYPES = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


class SpeechToTextError(RuntimeError):
    """Raised when speech-to-text cannot run."""


class SpeechToTextNotConfigured(SpeechToTextError):
    """Raised when no API key is saved for the provider voice input would use."""


def _runtime_stt_settings():
    from app.agent.settings_store import SpeechToTextSettings, load_agent_settings
    from app.config import settings as base_settings

    agent_settings = load_agent_settings(base_settings)
    stt_settings = getattr(agent_settings, "speech_to_text", None) or SpeechToTextSettings()
    chat_provider = str(getattr(getattr(agent_settings, "llm", None), "provider", "") or "")
    return stt_settings, base_settings, chat_provider


def _model_for(provider: str) -> str:
    return OPENAI_STT_MODEL if provider == "openai" else GEMINI_STT_MODEL if provider == "gemini" else ""


def speech_to_text_status() -> dict[str, Any]:
    stt_settings, base_settings, chat_provider = _runtime_stt_settings()
    preference = str(getattr(stt_settings, "provider", "") or "auto")
    active = resolve_key_provider(preference, saved_api_keys(base_settings), chat_provider)
    return {
        "provider": preference,
        "active_provider": active,
        "model": _model_for(active),
        "configured": bool(active),
    }


def _audio_mime_type(path: Path) -> str:
    return _AUDIO_MIME_TYPES.get(path.suffix.lower(), "audio/wav")


async def transcribe_audio_file(path: str | Path) -> str:
    audio_path = Path(path)
    if not audio_path.is_file():
        raise FileNotFoundError(str(audio_path))
    if audio_path.stat().st_size > MAX_TRANSCRIPTION_BYTES:
        raise ValueError("Audio file exceeds the 25 MB transcription limit.")
    stt_settings, base_settings, chat_provider = _runtime_stt_settings()
    preference = str(getattr(stt_settings, "provider", "") or "auto")
    keys = saved_api_keys(base_settings)
    provider = resolve_key_provider(preference, keys, chat_provider)
    if not provider:
        if preference == "openai":
            raise SpeechToTextNotConfigured("Voice input is set to OpenAI. Add an OpenAI API key in Settings > Connections.")
        if preference == "gemini":
            raise SpeechToTextNotConfigured("Voice input is set to Google. Add a Google API key in Settings > Connections.")
        raise SpeechToTextNotConfigured(VOICE_INPUT_NOT_CONFIGURED_MESSAGE)

    data = audio_path.read_bytes()
    mime_type = _audio_mime_type(audio_path)
    logger.info(
        "speech-to-text: transcribing provider=%s path=%s mime=%s bytes=%s",
        provider,
        audio_path,
        mime_type,
        len(data),
    )
    if provider == "openai":
        text = await _transcribe_openai(data, audio_path.name, mime_type, api_key=keys["openai"])
    else:
        text = await _transcribe_gemini(data, mime_type, api_key=keys["gemini"])
    logger.info("speech-to-text: result provider=%s text_chars=%s", provider, len(text))
    return text


async def _transcribe_openai(data: bytes, filename: str, mime_type: str, *, api_key: str) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=STT_REQUEST_TIMEOUT_SECONDS)
    try:
        response = await client.audio.transcriptions.create(
            model=OPENAI_STT_MODEL,
            file=(filename, data, mime_type),
        )
    finally:
        await client.close()
    return str(getattr(response, "text", "") or "").strip()


async def _transcribe_gemini(data: bytes, mime_type: str, *, api_key: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=STT_REQUEST_TIMEOUT_SECONDS * 1000),
    )
    prompt = (
        "Transcribe the spoken audio exactly. Output only the transcribed text, with no labels, "
        "formatting, explanations, or surrounding quotes. Keep the original spoken language and do not translate. "
        "If no speech is recognized, output an empty string."
    )
    response = await client.aio.models.generate_content(
        model=GEMINI_STT_MODEL,
        contents=[
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    types.Part.from_bytes(data=data, mime_type=mime_type),
                ],
            }
        ],
        config=types.GenerateContentConfig(
            response_mime_type="text/plain",
            temperature=0,
            # A 25 MB recording can run past ten minutes, and thinking would
            # spend the output budget before the transcript.
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return str(getattr(response, "text", "") or "").strip()
