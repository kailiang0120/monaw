import asyncio
import sys
import types

import pytest

from app.api.routes import speech_to_text as stt_route
from app.agent import speech_to_text as stt
from app.agent.api_key_providers import resolve_key_provider
from app.agent.settings_store import SpeechToTextSettings
from app.schemas import SpeechToTextTranscriptionRequest


def _use_settings(monkeypatch, *, provider="auto", openai_key="", google_key="", chat_provider="openai"):
    monkeypatch.setattr(
        stt,
        "_runtime_stt_settings",
        lambda: (
            types.SimpleNamespace(provider=provider),
            types.SimpleNamespace(openai_api_key=openai_key, google_api_key=google_key),
            chat_provider,
        ),
    )


@pytest.mark.parametrize(
    ("preference", "keys", "chat_provider", "expected"),
    [
        ("auto", {"openai": "sk", "gemini": "g"}, "gemini", "gemini"),
        ("auto", {"openai": "sk", "gemini": "g"}, "openai", "openai"),
        ("auto", {"openai": "sk", "gemini": "g"}, "codex", "openai"),
        ("auto", {"openai": "", "gemini": "g"}, "codex", "gemini"),
        ("auto", {"openai": "", "gemini": "g"}, "openai", "gemini"),
        ("auto", {"openai": "", "gemini": ""}, "openai", ""),
        ("openai", {"openai": "", "gemini": "g"}, "gemini", ""),
        ("gemini", {"openai": "sk", "gemini": "g"}, "openai", "gemini"),
    ],
)
def test_resolve_key_provider(preference, keys, chat_provider, expected):
    assert resolve_key_provider(preference, keys, chat_provider) == expected


def test_speech_to_text_status_reports_the_provider_in_use(monkeypatch):
    _use_settings(monkeypatch, google_key="g", chat_provider="codex")

    assert stt.speech_to_text_status() == {
        "provider": "auto",
        "active_provider": "gemini",
        "model": stt.GEMINI_STT_MODEL,
        "configured": True,
    }


def test_speech_to_text_status_without_keys_is_not_configured(monkeypatch):
    _use_settings(monkeypatch)

    status = stt.speech_to_text_status()

    assert status["configured"] is False
    assert status["active_provider"] == ""
    assert status["model"] == ""


def test_transcribe_audio_without_keys_names_both_providers(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice")
    _use_settings(monkeypatch)

    with pytest.raises(stt.SpeechToTextNotConfigured, match="OpenAI or Google API key"):
        asyncio.run(stt.transcribe_audio_file(audio_path))


def test_transcribe_audio_with_chosen_provider_missing_its_key(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice")
    _use_settings(monkeypatch, provider="openai", google_key="g")

    with pytest.raises(stt.SpeechToTextNotConfigured, match="set to OpenAI"):
        asyncio.run(stt.transcribe_audio_file(audio_path))


def test_transcribe_audio_sends_the_recording_to_openai(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"RIFF-voice")
    _use_settings(monkeypatch, openai_key="sk-test")
    calls = []

    class FakeTranscriptions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return types.SimpleNamespace(text="  hello there  ")

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            self.audio = types.SimpleNamespace(transcriptions=FakeTranscriptions())

        async def close(self):
            calls.append("closed")

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))

    assert asyncio.run(stt.transcribe_audio_file(audio_path)) == "hello there"
    assert calls[0]["api_key"] == "sk-test"
    assert calls[1] == {"model": stt.OPENAI_STT_MODEL, "file": ("voice.wav", b"RIFF-voice", "audio/wav")}
    assert calls[2] == "closed"


def test_transcribe_audio_sends_telegram_ogg_to_gemini_with_its_type(tmp_path, monkeypatch):
    audio_path = tmp_path / "telegram-voice.oga"
    audio_path.write_bytes(b"OggS-voice")
    _use_settings(monkeypatch, provider="gemini", openai_key="sk", google_key="g-test")
    from google.genai import models

    sent = {}

    async def fake_generate_content(self, *, model, contents, config):
        sent.update(model=model, part=contents[0]["parts"][1], config=config)
        return types.SimpleNamespace(text="kia ora\n")

    monkeypatch.setattr(models.AsyncModels, "generate_content", fake_generate_content)

    assert asyncio.run(stt.transcribe_audio_file(audio_path)) == "kia ora"
    assert sent["model"] == stt.GEMINI_STT_MODEL
    assert sent["part"].inline_data.mime_type == "audio/ogg"
    assert sent["part"].inline_data.data == b"OggS-voice"
    assert sent["config"].thinking_config.thinking_budget == 0


def test_settings_from_the_local_engine_migrate_to_a_cloud_provider():
    assert SpeechToTextSettings.model_validate({"engine": "local", "local_model": "base"}).provider == "auto"
    assert SpeechToTextSettings.model_validate(
        {"engine": "cloud", "cloud_provider": "gemini", "cloud_model": "gemini-2.5-flash"}
    ).provider == "gemini"
    assert SpeechToTextSettings.model_validate({"provider": "whisper"}).provider == "auto"
    assert SpeechToTextSettings().model_dump() == {"provider": "auto"}


def test_transcription_route_returns_clear_not_configured_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_route, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))

    async def fake_transcribe_audio_file(_path):
        raise stt.SpeechToTextNotConfigured(stt.VOICE_INPUT_NOT_CONFIGURED_MESSAGE)

    monkeypatch.setattr(stt_route, "transcribe_audio_file", fake_transcribe_audio_file)
    body = SpeechToTextTranscriptionRequest(
        filename="voice.wav",
        data_base64="dm9pY2U=",
        mime_type="audio/wav",
    )

    with pytest.raises(stt_route.HTTPException) as exc_info:
        asyncio.run(stt_route.transcribe_speech_to_text(body))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == stt.VOICE_INPUT_NOT_CONFIGURED_MESSAGE
    assert not list(tmp_path.rglob("*.*"))
