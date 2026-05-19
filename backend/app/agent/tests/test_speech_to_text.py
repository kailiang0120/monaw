import asyncio
import sys
import types

import pytest

from app.api.routes import speech_to_text as stt_route
from app.agent import speech_to_text as stt
from app.schemas import SpeechToTextTranscriptionRequest


def test_speech_to_text_status_reports_default_local_model(tmp_path, monkeypatch):
    model_dir = tmp_path / "base"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(stt, "stt_model_dir", lambda: model_dir)
    monkeypatch.setattr(stt, "_dependency_available", lambda: True)
    monkeypatch.setattr(
        stt,
        "_runtime_stt_settings",
        lambda: (
            types.SimpleNamespace(engine="local", local_model="base", cloud_model="gemini-2.5-flash"),
            types.SimpleNamespace(google_api_key=""),
        ),
    )

    status = stt.speech_to_text_status()

    assert status["provider"] == "faster-whisper"
    assert status["label"] == "Powered by faster-whisper"
    assert status["engine"] == "local"
    assert status["cloud_provider"] == "gemini"
    assert status["cloud_model"] == "gemini-2.5-flash"
    assert status["model_id"] == "base"
    assert status["model_label"] == "Whisper base"
    assert status["downloaded"] is True
    assert status["dependency_available"] is True
    assert status["loaded"] is False


def test_download_default_stt_model_uses_faster_whisper_utils(tmp_path, monkeypatch):
    model_dir = tmp_path / "base"

    def fake_download_model(model_id, *, output_dir, local_files_only):
        assert model_id == "base"
        assert local_files_only is False
        assert output_dir == str(model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / "model.bin").write_bytes(b"model")
        (model_dir / "config.json").write_text("{}", encoding="utf-8")

    fake_package = types.ModuleType("faster_whisper")
    fake_utils = types.ModuleType("faster_whisper.utils")
    fake_utils.download_model = fake_download_model
    fake_package.utils = fake_utils

    monkeypatch.setitem(sys.modules, "faster_whisper", fake_package)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", fake_utils)
    monkeypatch.setattr(stt, "stt_model_dir", lambda: model_dir)

    status = stt.download_default_stt_model()

    assert status["downloaded"] is True
    assert status["download_dir"] == str(model_dir)


def test_delete_stt_model_offloads_and_removes_model_dir(tmp_path, monkeypatch):
    model_dir = tmp_path / "base"
    model_dir.mkdir()
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    stt._MODEL_CACHE[(str(model_dir), "cpu", "int8")] = object()
    monkeypatch.setattr(stt, "stt_model_dir", lambda: model_dir)
    monkeypatch.setattr(stt, "_dependency_available", lambda: True)

    status = stt.delete_stt_model()

    assert not model_dir.exists()
    assert stt._MODEL_CACHE == {}
    assert status["downloaded"] is False
    assert status["loaded"] is False


def test_offload_stt_model_clears_loaded_model_cache(monkeypatch):
    stt._MODEL_CACHE[("path", "cpu", "int8")] = object()
    monkeypatch.setattr(stt, "is_stt_model_downloaded", lambda: True)
    monkeypatch.setattr(stt, "_dependency_available", lambda: True)

    status = stt.offload_stt_model()

    assert stt._MODEL_CACHE == {}
    assert status["loaded"] is False


def test_transcribe_audio_requires_downloaded_local_model(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice")
    monkeypatch.setattr(stt, "stt_model_dir", lambda: tmp_path / "missing-model")
    monkeypatch.setattr(
        stt,
        "_runtime_stt_settings",
        lambda: (
            types.SimpleNamespace(engine="local", local_model="base", cloud_model="gemini-2.5-flash"),
            types.SimpleNamespace(google_api_key=""),
        ),
    )

    with pytest.raises(stt.SpeechToTextModelMissing, match="not installed/configured"):
        asyncio.run(stt.transcribe_audio_file(audio_path))


def test_transcribe_audio_requires_google_key_for_cloud_engine(tmp_path, monkeypatch):
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"voice")
    monkeypatch.setattr(
        stt,
        "_runtime_stt_settings",
        lambda: (
            types.SimpleNamespace(engine="cloud", cloud_model="gemini-2.5-flash"),
            types.SimpleNamespace(google_api_key=""),
        ),
    )

    with pytest.raises(stt.SpeechToTextCloudNotConfigured, match="Google API key"):
        asyncio.run(stt.transcribe_audio_file(audio_path))


def test_transcription_route_returns_clear_missing_model_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_route, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))

    async def fake_transcribe_audio_file(_path):
        raise stt.SpeechToTextModelMissing(stt.VOICE_MODEL_NOT_READY_MESSAGE)

    monkeypatch.setattr(stt_route, "transcribe_audio_file", fake_transcribe_audio_file)
    body = SpeechToTextTranscriptionRequest(
        filename="voice.webm",
        data_base64="dm9pY2U=",
        mime_type="audio/webm",
    )

    with pytest.raises(stt_route.HTTPException) as exc_info:
        asyncio.run(stt_route.transcribe_speech_to_text(body))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == stt.VOICE_MODEL_NOT_READY_MESSAGE
    assert not list(tmp_path.rglob("*.*"))


def test_transcription_route_returns_clear_cloud_config_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stt_route, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))

    async def fake_transcribe_audio_file(_path):
        raise stt.SpeechToTextCloudNotConfigured("Google API key is required for cloud speech-to-text.")

    monkeypatch.setattr(stt_route, "transcribe_audio_file", fake_transcribe_audio_file)
    body = SpeechToTextTranscriptionRequest(
        filename="voice.webm",
        data_base64="dm9pY2U=",
        mime_type="audio/webm",
    )

    with pytest.raises(stt_route.HTTPException) as exc_info:
        asyncio.run(stt_route.transcribe_speech_to_text(body))

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Google API key is required for cloud speech-to-text."
    assert not list(tmp_path.rglob("*.*"))
