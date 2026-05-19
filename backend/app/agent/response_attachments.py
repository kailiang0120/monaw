from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.agent.runtime_paths import RUNTIME_DIR

MAX_RESPONSE_ATTACHMENTS = 12
MIN_SCREENSHOT_PREVIEW_DIMENSION_PX = 16
MAX_GENERATED_SCREENSHOT_DIMENSION_PX = 100_000
MAX_GENERATED_SCREENSHOT_PIXELS = 120_000_000
_ATTACHMENT_ID_SALT = secrets.token_bytes(16)
_ATTACHMENT_REGISTRY: dict[str, Path] = {}
_ATTACHMENT_REGISTRY_LOADED = False
_ATTACHMENT_REGISTRY_PATH = RUNTIME_DIR / "attachment_registry.json"
_GENERATED_SCREENSHOT_NAME_RE = re.compile(
    r"(?:^|_)(?:browser|desktop|screen|screenshot|snapshot)(?:_|$)", re.IGNORECASE
)

_WINDOWS_FILE_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:\\|\\\\)(?:[^<>:\"|?*\r\n]+\\)+[^<>:\"|?*\r\n]+?\.[A-Za-z0-9]{1,10})(?=$|[\s)\],.;:'\"`<>])"
)
_PATH_KEYS = {
    "path",
    "file",
    "filename",
    "filepath",
    "file_path",
    "output",
    "output_path",
    "saved_path",
    "download_path",
    "screenshot_path",
    "image_path",
    "document_path",
}
_RETURNABLE_EXTENSIONS = {
    ".apng",
    ".avif",
    ".bmp",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".svg",
    ".txt",
    ".webp",
    ".xls",
    ".xlsx",
    ".xml",
    ".zip",
}


def _stable_id(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(_ATTACHMENT_ID_SALT)
    digest.update(str(path).encode("utf-8"))
    return digest.hexdigest()[:24]


def _load_attachment_registry() -> None:
    global _ATTACHMENT_REGISTRY_LOADED
    if _ATTACHMENT_REGISTRY_LOADED:
        return
    _ATTACHMENT_REGISTRY_LOADED = True
    try:
        payload = json.loads(_ATTACHMENT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if not isinstance(payload, dict):
        return
    for attachment_id, raw_path in payload.items():
        try:
            target = Path(str(raw_path)).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if target.is_file():
            _ATTACHMENT_REGISTRY[str(attachment_id)] = target


def _persist_attachment_registry() -> None:
    try:
        _ATTACHMENT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            attachment_id: str(path)
            for attachment_id, path in sorted(_ATTACHMENT_REGISTRY.items())
            if path.is_file()
        }
        tmp_path = _ATTACHMENT_REGISTRY_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(_ATTACHMENT_REGISTRY_PATH)
    except OSError:
        return


def register_attachment_path(attachment_id: str, path: str | Path) -> None:
    _load_attachment_registry()
    try:
        target = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return
    if target.is_file():
        _ATTACHMENT_REGISTRY[str(attachment_id)] = target
        _persist_attachment_registry()


def resolve_attachment_path(attachment_id: str) -> Path | None:
    _load_attachment_registry()
    path = _ATTACHMENT_REGISTRY.get(str(attachment_id))
    if path is None or not path.is_file():
        return None
    return path


def _strip_path_text(value: str) -> str:
    return value.strip().strip("`'\"<>").rstrip(".,;:)]}")


def _resolved_candidate_path(raw_path: str) -> Path | None:
    cleaned = _strip_path_text(raw_path)
    if not cleaned:
        return None
    try:
        return Path(os.path.expandvars(cleaned)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _attachment_from_resolved_path(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    if path.suffix.lower() not in _RETURNABLE_EXTENSIONS:
        return None
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    image_dimensions = get_image_dimensions(path) if mime_type.startswith("image/") else None
    if _is_invalid_generated_screenshot(path, image_dimensions):
        _delete_file(path)
        return None
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    attachment_id = _stable_id(path)
    register_attachment_path(attachment_id, path)
    attachment = {
        "id": attachment_id,
        "name": path.name,
        "path": str(path),
        "mime_type": mime_type,
        "size": size,
    }
    if image_dimensions is not None:
        attachment["width"], attachment["height"] = image_dimensions
    return attachment


def _attachment_from_path(raw_path: str) -> dict[str, Any] | None:
    path = _resolved_candidate_path(raw_path)
    if path is None:
        return None
    return _attachment_from_resolved_path(path)


def _delete_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def _is_generated_screenshot_name(path: Path) -> bool:
    name = path.stem.replace("-", "_")
    return bool(_GENERATED_SCREENSHOT_NAME_RE.search(name))


def _is_generated_screenshot_attachment_path(path: Path) -> bool:
    mime_type = mimetypes.guess_type(str(path))[0] or ""
    return mime_type.startswith("image/") and _is_generated_screenshot_name(path)


def _is_invalid_generated_screenshot(
    path: Path,
    dimensions: tuple[int, int] | None,
) -> bool:
    if dimensions is None or not _is_generated_screenshot_name(path):
        return False
    width, height = dimensions
    return (
        width < MIN_SCREENSHOT_PREVIEW_DIMENSION_PX
        or height < MIN_SCREENSHOT_PREVIEW_DIMENSION_PX
        or width > MAX_GENERATED_SCREENSHOT_DIMENSION_PX
        or height > MAX_GENERATED_SCREENSHOT_DIMENSION_PX
        or width * height > MAX_GENERATED_SCREENSHOT_PIXELS
    )


def get_image_dimensions(path: str | Path) -> tuple[int, int] | None:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return _image_dimensions_from_bytes(data)


def _image_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if len(data) >= 26 and data[:2] == b"BM":
        width = int.from_bytes(data[18:22], "little", signed=True)
        height = int.from_bytes(data[22:26], "little", signed=True)
        return abs(width), abs(height)
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        return _jpeg_dimensions_from_bytes(data)
    return None


def _jpeg_dimensions_from_bytes(data: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA or index + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            if segment_length < 7:
                return None
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += segment_length
    return None


def _paths_from_text(text: str) -> Iterable[str]:
    for match in _WINDOWS_FILE_RE.finditer(str(text or "")):
        yield match.group("path")


def _paths_from_json_value(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_normalized = str(key).lower()
            if isinstance(child, str):
                if key_normalized in _PATH_KEYS or key_normalized.endswith("_path"):
                    yield child
            else:
                yield from _paths_from_json_value(child)
    elif isinstance(value, list):
        for item in value:
            yield from _paths_from_json_value(item)
    elif isinstance(value, str):
        yield from _paths_from_text(value)


def _paths_from_tool_output(output: str) -> Iterable[str]:
    text = str(output or "")
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return list(_paths_from_text(text))
    return list(_paths_from_json_value(parsed))


def collect_response_attachments(
    *,
    content: str = "",
    explicit_paths: Iterable[str] | None = None,
    tool_calls: Iterable[dict[str, Any]] | None = None,
    tool_outputs: Iterable[str] | None = None,
    limit: int = MAX_RESPONSE_ATTACHMENTS,
    include_generated_tool_screenshots: bool = False,
) -> list[dict[str, Any]]:
    """Collect user-visible deliverables without exposing progress screenshots.

    Explicit final-answer paths and paths mentioned in final assistant content
    are returned. Paths discovered only inside tool outputs are treated as
    background artifacts when their filename looks like a generated
    browser/desktop screenshot.
    """
    seen: set[str] = set()
    attachments: list[dict[str, Any]] = []

    def add(raw_path: str, *, from_tool_output: bool = False) -> None:
        if len(attachments) >= limit:
            return
        path = _resolved_candidate_path(raw_path)
        if path is None:
            return
        if (
            from_tool_output
            and not include_generated_tool_screenshots
            and _is_generated_screenshot_attachment_path(path)
        ):
            return
        attachment = _attachment_from_resolved_path(path)
        if attachment is None:
            return
        key = os.path.normcase(os.path.normpath(attachment["path"]))
        if key in seen:
            return
        seen.add(key)
        attachments.append(attachment)

    for raw_path in explicit_paths or []:
        add(raw_path)

    for raw_path in _paths_from_text(content):
        add(raw_path)

    for tool_call in tool_calls or []:
        output = tool_call.get("output", "")
        for raw_path in _paths_from_tool_output(str(output or "")):
            add(raw_path, from_tool_output=True)

    for output in tool_outputs or []:
        for raw_path in _paths_from_tool_output(str(output or "")):
            add(raw_path, from_tool_output=True)

    return attachments
