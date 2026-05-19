from io import BytesIO

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError

from app.agent.response_attachments import resolve_attachment_path

router = APIRouter()
_PREVIEW_MAX_SIZE = (640, 480)


@router.get("/files/{attachment_id}")
async def get_local_file(attachment_id: str = Path(..., min_length=1)):
    target = resolve_attachment_path(attachment_id)
    if target is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(target), filename=target.name, content_disposition_type="inline")


@router.get("/files/{attachment_id}/preview")
async def get_local_file_preview(attachment_id: str = Path(..., min_length=1)):
    target = resolve_attachment_path(attachment_id)
    if target is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        with Image.open(target) as image:
            image.thumbnail(_PREVIEW_MAX_SIZE)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            output = BytesIO()
            image.save(output, format="PNG")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(status_code=422, detail="Preview unavailable") from None
    return Response(
        output.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )
