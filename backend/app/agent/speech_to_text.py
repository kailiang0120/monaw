"""Local speech-to-text support powered by faster-whisper."""

from __future__ import annotations

import asyncio
import gc
import logging
import math
import shutil
import subprocess
import wave
from array import array
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import MONAW_HOME_DIR

logger = logging.getLogger(__name__)

STT_PROVIDER = "faster-whisper"
DEFAULT_STT_MODEL = "base"
DEFAULT_STT_MODEL_LABEL = "Whisper base"
DEFAULT_STT_MODEL_SIZE = "~142 MB"
DEFAULT_STT_ENGINE = "local"
DEFAULT_CLOUD_STT_PROVIDER = "gemini"
DEFAULT_CLOUD_STT_MODEL = "gemini-2.5-flash"
GEMINI_AUDIO_FALLBACK_MODEL = "gemini-2.5-flash"
DEFAULT_STT_DEVICE = "cpu"
DEFAULT_STT_COMPUTE_TYPE = "int8"
MAX_TRANSCRIPTION_BYTES = 25 * 1024 * 1024
VOICE_MODEL_NOT_READY_MESSAGE = (
    "Voice model is not installed/configured yet. Open Settings > Model > Speech to Text and download the model first."
)

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


class SpeechToTextError(RuntimeError):
    """Raised when local speech-to-text cannot run."""


class SpeechToTextModelMissing(SpeechToTextError):
    """Raised when the local STT model has not been downloaded."""


class SpeechToTextDependencyMissing(SpeechToTextError):
    """Raised when faster-whisper is not installed in the backend environment."""


class SpeechToTextCloudNotConfigured(SpeechToTextError):
    """Raised when cloud STT is selected without the required API key."""


def _runtime_stt_settings():
    from app.agent.settings_store import SpeechToTextSettings, load_agent_settings
    from app.config import settings as base_settings

    agent_settings = load_agent_settings(base_settings)
    return getattr(agent_settings, "speech_to_text", None) or SpeechToTextSettings(), base_settings


def _local_model_id() -> str:
    stt_settings, _base_settings = _runtime_stt_settings()
    return str(getattr(stt_settings, "local_model", "") or DEFAULT_STT_MODEL).strip() or DEFAULT_STT_MODEL


def stt_model_dir(model_id: str | None = None) -> Path:
    return MONAW_HOME_DIR / "models" / STT_PROVIDER / (model_id or _local_model_id())


def is_stt_model_downloaded() -> bool:
    model_dir = stt_model_dir()
    return (model_dir / "model.bin").is_file() and (model_dir / "config.json").is_file()


def _dependency_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def speech_to_text_status() -> dict[str, Any]:
    stt_settings, base_settings = _runtime_stt_settings()
    engine = str(getattr(stt_settings, "engine", "") or DEFAULT_STT_ENGINE)
    cloud_model = str(getattr(stt_settings, "cloud_model", "") or DEFAULT_CLOUD_STT_MODEL)
    local_model = str(getattr(stt_settings, "local_model", "") or DEFAULT_STT_MODEL)
    return {
        "provider": STT_PROVIDER,
        "label": "Powered by faster-whisper",
        "engine": engine,
        "cloud_provider": DEFAULT_CLOUD_STT_PROVIDER,
        "cloud_model": cloud_model,
        "cloud_configured": bool(getattr(base_settings, "google_api_key", "")),
        "model_id": local_model,
        "model_label": "Whisper base" if local_model == "base" else f"Whisper {local_model}",
        "model_size": DEFAULT_STT_MODEL_SIZE if local_model == "base" else "",
        "downloaded": is_stt_model_downloaded(),
        "download_dir": str(stt_model_dir()),
        "dependency_available": _dependency_available(),
        "loaded": bool(_MODEL_CACHE),
    }


def offload_stt_model() -> dict[str, Any]:
    loaded_count = len(_MODEL_CACHE)
    _MODEL_CACHE.clear()
    gc.collect()
    logger.info("speech-to-text: offloaded model cache entries=%s", loaded_count)
    return speech_to_text_status()


def download_default_stt_model() -> dict[str, Any]:
    try:
        from faster_whisper.utils import download_model
    except Exception as exc:
        raise SpeechToTextDependencyMissing(
            "faster-whisper is not installed. Install backend requirements before downloading the speech-to-text model."
        ) from exc

    model_dir = stt_model_dir()
    model_dir.mkdir(parents=True, exist_ok=True)
    download_model(_local_model_id(), output_dir=str(model_dir), local_files_only=False)
    return speech_to_text_status()


def delete_stt_model() -> dict[str, Any]:
    offload_stt_model()
    model_dir = stt_model_dir()
    if model_dir.exists():
        shutil.rmtree(model_dir, ignore_errors=True)
        logger.info("speech-to-text: deleted local model dir=%s", model_dir)
    return speech_to_text_status()


def _ffmpeg_executable() -> str:
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return ""


def _wav_audio_stats(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            frames = wav_file.readframes(frame_count)
    except Exception as exc:
        return {"error": str(exc)}

    duration = frame_count / frame_rate if frame_rate else 0
    stats: dict[str, Any] = {
        "duration_sec": round(duration, 3),
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": frame_rate,
        "frames": frame_count,
    }
    if sample_width != 2 or not frames:
        return stats

    samples = array("h")
    samples.frombytes(frames)
    if not samples:
        return stats
    peak = max(abs(sample) for sample in samples)
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    dbfs = 20 * math.log10(rms / 32768) if rms > 0 else -120.0
    stats.update({
        "peak": peak,
        "rms": round(rms, 2),
        "dbfs": round(dbfs, 2),
    })
    return stats


def _transcription_input_path(path: Path) -> tuple[Path, Path | None]:
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        if path.suffix.lower() == ".wav":
            logger.info(
                "speech-to-text: ffmpeg unavailable; using direct wav input path=%s bytes=%s audio_stats=%s",
                path,
                path.stat().st_size,
                _wav_audio_stats(path),
            )
            return path, None
        logger.warning(
            "speech-to-text: ffmpeg unavailable; passing original audio to faster-whisper path=%s suffix=%s bytes=%s",
            path,
            path.suffix,
            path.stat().st_size,
        )
        return path, None

    converted = path.with_name(f"{path.stem}.transcription.wav")
    logger.info(
        "speech-to-text: converting audio path=%s suffix=%s bytes=%s ffmpeg=%s",
        path,
        path.suffix,
        path.stat().st_size,
        ffmpeg,
    )
    try:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(path),
                "-vn",
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-acodec",
                "pcm_s16le",
                str(converted),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        logger.error(
            "speech-to-text: ffmpeg conversion failed returncode=%s stderr=%s",
            exc.returncode,
            (exc.stderr or "").strip()[-1000:],
        )
        raise
    logger.info(
        "speech-to-text: converted audio path=%s bytes=%s audio_stats=%s",
        converted,
        converted.stat().st_size if converted.exists() else 0,
        _wav_audio_stats(converted) if converted.exists() else {},
    )
    return converted, converted


def _load_model():
    if not is_stt_model_downloaded():
        raise SpeechToTextModelMissing(VOICE_MODEL_NOT_READY_MESSAGE)
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise SpeechToTextDependencyMissing(
            "faster-whisper is not installed. Install backend requirements before using speech-to-text."
        ) from exc

    model_dir = stt_model_dir()
    cache_key = (str(model_dir), DEFAULT_STT_DEVICE, DEFAULT_STT_COMPUTE_TYPE)
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = WhisperModel(
            str(model_dir),
            device=DEFAULT_STT_DEVICE,
            compute_type=DEFAULT_STT_COMPUTE_TYPE,
        )
        _MODEL_CACHE[cache_key] = model
    return model


def _transcribe_audio_file_sync(path: Path) -> str:
    model = _load_model()
    input_path, temporary_path = _transcription_input_path(path)
    try:
        logger.info(
            "speech-to-text: transcribing input_path=%s original_path=%s input_bytes=%s original_bytes=%s",
            input_path,
            path,
            input_path.stat().st_size if input_path.exists() else 0,
            path.stat().st_size if path.exists() else 0,
        )
        segments, _info = model.transcribe(
            str(input_path),
            beam_size=1,
        )
        segment_list = list(segments)
        text = " ".join(segment.text.strip() for segment in segment_list if segment.text.strip()).strip()
        logger.info(
            "speech-to-text: result segments=%s text_chars=%s duration=%s language=%s language_probability=%s",
            len(segment_list),
            len(text),
            getattr(_info, "duration", ""),
            getattr(_info, "language", ""),
            getattr(_info, "language_probability", ""),
        )
        if not text:
            logger.warning(
                "speech-to-text: empty transcript input_path=%s input_bytes=%s duration=%s segments=%s",
                input_path,
                input_path.stat().st_size if input_path.exists() else 0,
                getattr(_info, "duration", ""),
                len(segment_list),
            )
        return text
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


async def transcribe_audio_file(path: str | Path) -> str:
    audio_path = Path(path)
    if not audio_path.is_file():
        raise FileNotFoundError(str(audio_path))
    if audio_path.stat().st_size > MAX_TRANSCRIPTION_BYTES:
        raise ValueError("Audio file exceeds the 25 MB transcription limit.")
    stt_settings, base_settings = _runtime_stt_settings()
    engine = str(getattr(stt_settings, "engine", "") or DEFAULT_STT_ENGINE)
    if engine == "cloud":
        return await _transcribe_audio_file_gemini(audio_path, stt_settings=stt_settings, base_settings=base_settings)
    return await asyncio.to_thread(_transcribe_audio_file_sync, audio_path)


async def _transcribe_audio_file_gemini(path: Path, *, stt_settings, base_settings) -> str:
    api_key = str(getattr(base_settings, "google_api_key", "") or "").strip()
    if not api_key:
        raise SpeechToTextCloudNotConfigured("Google API key is required for cloud speech-to-text.")

    from google import genai
    from google.genai import types

    input_path, temporary_path = _transcription_input_path(path)
    try:
        data = input_path.read_bytes()
        client = genai.Client(api_key=api_key)
        prompt = (
            "Transcribe the spoken audio exactly. Output only the transcribed text, with no labels, "
            "formatting, explanations, or surrounding quotes. Keep the original spoken language and do not translate. "
            "If no speech is recognized, output an empty string."
        )
        contents = [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    types.Part.from_bytes(data=data, mime_type="audio/wav"),
                ],
            }
        ]
        preferred_model = str(getattr(stt_settings, "cloud_model", "") or DEFAULT_CLOUD_STT_MODEL)
        models = [preferred_model]
        if preferred_model != GEMINI_AUDIO_FALLBACK_MODEL:
            models.append(GEMINI_AUDIO_FALLBACK_MODEL)
        last_error: Exception | None = None
        for model_name in models:
            try:
                logger.info(
                    "speech-to-text: cloud gemini transcribing model=%s input_path=%s bytes=%s",
                    model_name,
                    input_path,
                    len(data),
                )
                response = await client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="text/plain",
                        temperature=0,
                        max_output_tokens=1000,
                    ),
                )
                text = str(getattr(response, "text", "") or "").strip()
                logger.info("speech-to-text: cloud gemini result model=%s text_chars=%s", model_name, len(text))
                return text
            except Exception as exc:
                last_error = exc
                logger.warning("speech-to-text: cloud gemini failed model=%s error=%s", model_name, exc)
        if last_error is not None:
            raise last_error
        return ""
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
