"""Save MCP image content blocks as files instead of returning base64 to the model."""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent.output_workspace import configured_screenshots_dir
from app.agent.response_attachments import get_image_dimensions, screenshot_dimension_problem

_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_SAFE_RE = re.compile(r"[^a-zA-Z0-9]+")


def _slug(value: str) -> str:
    return _SAFE_RE.sub("_", value).strip("_").lower()[:40] or "tool"


def _image_path(directory: Path, server: str, tool: str, extension: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # "screenshot" in the name keeps these out of final-answer attachments
    # unless the user asked for them, like Monaw's other browser screenshots.
    name = f"mcp_screenshot_{_slug(server)}_{_slug(tool)}_{stamp}_{uuid.uuid4().hex[:8]}.{extension}"
    return directory / name


def externalize_image_blocks(
    content: list[Any], *, server: str, tool: str, directory: Path | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Write each base64 image block to disk and replace it with its path.

    Returns the rewritten content and the saved images ({path, mime_type, width, height}).
    """
    if not any(isinstance(item, dict) and item.get("type") == "image" for item in content):
        return content, []
    target_dir = directory or configured_screenshots_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    rewritten: list[Any] = []
    images: list[dict[str, Any]] = []
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "image"):
            rewritten.append(item)
            continue
        mime_type = str(item.get("mimeType") or item.get("mime_type") or "image/png").lower()
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            data = b""
        if not data:
            rewritten.append({"type": "image", "mime_type": mime_type, "skipped": {"reason_code": "invalid_image_data"}})
            continue
        path = _image_path(target_dir, server, tool, _EXTENSIONS.get(mime_type, "png"))
        path.write_bytes(data)
        dimensions = get_image_dimensions(path)
        problem = screenshot_dimension_problem(*dimensions) if dimensions else ""
        if problem:
            path.unlink(missing_ok=True)
            rewritten.append({
                "type": "image",
                "mime_type": mime_type,
                "skipped": {"reason_code": problem, "width": dimensions[0], "height": dimensions[1]},
            })
            continue
        width, height = dimensions if dimensions else (None, None)
        images.append({"path": str(path), "mime_type": mime_type, "width": width, "height": height})
        rewritten.append({
            "type": "image",
            "mime_type": mime_type,
            "image_path": str(path),
            "width": width,
            "height": height,
        })
    return rewritten, images
