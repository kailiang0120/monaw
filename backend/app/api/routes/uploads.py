import base64
import binascii
import re
import uuid

from fastapi import APIRouter, HTTPException

from app.agent.response_attachments import register_attachment_path
from app.agent.runtime_paths import runtime_path
from app.schemas import AttachmentUploadRequest, AttachmentUploadResponse

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(filename: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", filename).strip(" .")
    return cleaned[:160] or "upload"


@router.post("/uploads", response_model=AttachmentUploadResponse)
async def upload_attachment(body: AttachmentUploadRequest):
    upload_id = str(uuid.uuid4())
    encoded = body.data_base64
    if "," in encoded and encoded.lstrip().lower().startswith("data:"):
        encoded = encoded.split(",", 1)[1]

    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 file payload") from exc

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Uploaded file exceeds 50 MB limit")

    safe_name = _safe_filename(body.filename)
    conv_dir = body.conversation_id or "pending"
    target_path = runtime_path("uploads", conv_dir, upload_id, safe_name)
    target_path.write_bytes(data)
    register_attachment_path(upload_id, target_path)

    return AttachmentUploadResponse(
        id=upload_id,
        name=safe_name,
        path=str(target_path),
        mime_type=body.mime_type or "",
        size=len(data),
    )
