import base64
import binascii
import re

from fastapi import APIRouter, HTTPException, Request

from app.agent.response_attachments import (
    new_attachment_id,
    public_attachment_payload,
    register_attachment_path,
)
from app.agent.runtime_paths import runtime_path
from app.schemas import AttachmentUploadRequest, AttachmentUploadResponse

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(filename: str) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("_", filename).strip(" .")
    return cleaned[:160] or "upload"


@router.post("/uploads", response_model=AttachmentUploadResponse)
async def upload_attachment(body: AttachmentUploadRequest, request: Request):
    upload_id = new_attachment_id()
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
    control_session_id = request.state.control_session.session_id
    record = register_attachment_path(
        upload_id,
        target_path,
        control_session_id=control_session_id,
        principal_id=control_session_id,
        conversation_id=body.conversation_id or "pending",
        mime_type=body.mime_type or "",
    )
    if record is None:
        raise HTTPException(status_code=500, detail="Unable to register uploaded file")

    payload = public_attachment_payload(record)
    return AttachmentUploadResponse(**payload)
