import base64
import binascii
import logging
import re
import uuid

from fastapi import APIRouter, HTTPException

from app.agent.runtime_paths import runtime_path
from app.agent.speech_to_text import (
    MAX_TRANSCRIPTION_BYTES,
    SpeechToTextCloudNotConfigured,
    SpeechToTextDependencyMissing,
    SpeechToTextModelMissing,
    transcribe_audio_file,
)
from app.schemas import SpeechToTextTranscriptionRequest, SpeechToTextTranscriptionResponse

router = APIRouter()
logger = logging.getLogger(__name__)

_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(filename: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(filename or "")).strip(" .")
    return cleaned[:160] or "voice-input.webm"


@router.post("/speech-to-text/transcribe", response_model=SpeechToTextTranscriptionResponse)
async def transcribe_speech_to_text(body: SpeechToTextTranscriptionRequest):
    request_id = uuid.uuid4().hex[:12]
    encoded = body.data_base64
    if "," in encoded and encoded.lstrip().lower().startswith("data:"):
        encoded = encoded.split(",", 1)[1]

    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.warning("speech-to-text[%s]: invalid base64 filename=%s mime=%s", request_id, body.filename, body.mime_type)
        raise HTTPException(status_code=400, detail="Invalid base64 audio payload") from exc

    if len(data) > MAX_TRANSCRIPTION_BYTES:
        logger.warning(
            "speech-to-text[%s]: payload too large filename=%s mime=%s bytes=%s",
            request_id,
            body.filename,
            body.mime_type,
            len(data),
        )
        raise HTTPException(status_code=413, detail="Audio file exceeds the 25 MB transcription limit.")

    target_path = runtime_path("speech-to-text", request_id, _safe_filename(body.filename))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    try:
        logger.info(
            "speech-to-text[%s]: received filename=%s safe_name=%s mime=%s bytes=%s path=%s",
            request_id,
            body.filename,
            target_path.name,
            body.mime_type,
            len(data),
            target_path,
        )
        text = await transcribe_audio_file(target_path)
    except SpeechToTextModelMissing as exc:
        logger.warning("speech-to-text[%s]: model missing: %s", request_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SpeechToTextDependencyMissing as exc:
        logger.warning("speech-to-text[%s]: dependency missing: %s", request_id, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SpeechToTextCloudNotConfigured as exc:
        logger.warning("speech-to-text[%s]: cloud not configured: %s", request_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        logger.exception("speech-to-text[%s]: transcription failed", request_id)
        raise
    finally:
        target_path.unlink(missing_ok=True)
        try:
            target_path.parent.rmdir()
        except OSError:
            pass

    logger.info("speech-to-text[%s]: completed text_chars=%s", request_id, len(text))
    return SpeechToTextTranscriptionResponse(text=text)
