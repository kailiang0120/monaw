import asyncio
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from pathlib import Path as FilePath
import re

from fastapi import APIRouter, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError

from app.agent.response_attachments import (
    MAX_ATTACHMENT_DOWNLOAD_BYTES,
    revalidate_attachment_path,
    resolve_attachment_path,
)

router = APIRouter()
_PREVIEW_MAX_SIZE = (640, 480)
_PREVIEW_MAX_PIXELS = 50_000_000
_PREVIEW_TIMEOUT_SECONDS = 5
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_PREVIEW_EXECUTOR: ProcessPoolExecutor | None = None


def _safe_download_filename(value: str) -> str:
    cleaned = _FILENAME_SAFE_RE.sub("_", str(value or "")).strip(" .")
    return cleaned[:180] or "attachment"


def _preview_executor() -> ProcessPoolExecutor:
    global _PREVIEW_EXECUTOR
    if _PREVIEW_EXECUTOR is None:
        _PREVIEW_EXECUTOR = ProcessPoolExecutor(max_workers=1)
    return _PREVIEW_EXECUTOR


def _render_preview_png(path: str) -> bytes:
    Image.MAX_IMAGE_PIXELS = _PREVIEW_MAX_PIXELS
    with Image.open(path) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > _PREVIEW_MAX_PIXELS:
            raise ValueError("image dimensions exceed preview limits")
        image.thumbnail(_PREVIEW_MAX_SIZE)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def _authorized_attachment_path(
    request: Request,
    attachment_id: str,
    conversation_id: str,
) -> FilePath:
    control_session_id = request.state.control_session.session_id
    target = resolve_attachment_path(
        attachment_id,
        control_session_id=control_session_id,
        principal_id=control_session_id,
        conversation_id=conversation_id,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="File not found")
    revalidated = revalidate_attachment_path(target)
    if revalidated is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        if revalidated.stat().st_size > MAX_ATTACHMENT_DOWNLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File exceeds download size limit")
    except OSError:
        raise HTTPException(status_code=404, detail="File not found") from None
    return revalidated


@router.get("/files/{attachment_id}")
async def get_local_file(
    request: Request,
    attachment_id: str = PathParam(..., min_length=1),
    conversation_id: str = Query("", max_length=128),
):
    target = _authorized_attachment_path(request, attachment_id, conversation_id)
    return FileResponse(
        str(target),
        filename=_safe_download_filename(target.name),
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/files/{attachment_id}/preview")
async def get_local_file_preview(
    request: Request,
    attachment_id: str = PathParam(..., min_length=1),
    conversation_id: str = Query("", max_length=128),
):
    target = _authorized_attachment_path(request, attachment_id, conversation_id)
    try:
        loop = asyncio.get_running_loop()
        preview = await asyncio.wait_for(
            loop.run_in_executor(_preview_executor(), _render_preview_png, str(target)),
            timeout=_PREVIEW_TIMEOUT_SECONDS,
        )
    except (
        OSError,
        ValueError,
        TimeoutError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(status_code=422, detail="Preview unavailable") from None
    return Response(
        preview,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
