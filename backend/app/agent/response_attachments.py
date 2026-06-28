from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import stat
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
import time
from typing import Any

from app.agent.run_context import (
    current_control_session_id,
    current_conversation_id,
    current_principal_id,
)
from app.agent.runtime_paths import RUNTIME_DIR

MAX_RESPONSE_ATTACHMENTS = 12
MAX_ATTACHMENT_REGISTRY_ITEMS = 1000
ATTACHMENT_EXPIRY_SECONDS = 7 * 24 * 60 * 60
MAX_ATTACHMENT_DOWNLOAD_BYTES = 100 * 1024 * 1024
ATTACHMENT_HANDLE_PREFIX = "attachment://"
MIN_SCREENSHOT_PREVIEW_DIMENSION_PX = 16
MAX_GENERATED_SCREENSHOT_DIMENSION_PX = 100_000
MAX_GENERATED_SCREENSHOT_PIXELS = 120_000_000
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


@dataclass(slots=True)
class AttachmentRecord:
    id: str
    path: str
    name: str
    mime_type: str = "application/octet-stream"
    size: int = 0
    width: int | None = None
    height: int | None = None
    control_session_id: str = ""
    principal_id: str = ""
    conversation_id: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0


_ATTACHMENT_REGISTRY: dict[str, AttachmentRecord] = {}


def new_attachment_id() -> str:
    return secrets.token_urlsafe(18)


def attachment_handle(attachment_id: str) -> str:
    return f"{ATTACHMENT_HANDLE_PREFIX}{attachment_id}"


def _attachment_id_from_handle(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith(ATTACHMENT_HANDLE_PREFIX):
        return text[len(ATTACHMENT_HANDLE_PREFIX):].strip()
    return text


def public_attachment_payload(record: AttachmentRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": record.id,
        "name": record.name,
        "path": attachment_handle(record.id),
        "mime_type": record.mime_type,
        "size": record.size,
        "conversation_id": record.conversation_id,
        "expires_at": int(record.expires_at),
    }
    if record.width is not None:
        payload["width"] = record.width
    if record.height is not None:
        payload["height"] = record.height
    return payload


def _record_from_path(
    attachment_id: str,
    target: Path,
    *,
    control_session_id: str = "",
    principal_id: str = "",
    conversation_id: str = "",
    mime_type: str = "",
    width: int | None = None,
    height: int | None = None,
    expires_in: int = ATTACHMENT_EXPIRY_SECONDS,
) -> AttachmentRecord:
    resolved_mime = mime_type or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    now = time.time()
    return AttachmentRecord(
        id=str(attachment_id),
        path=str(target),
        name=target.name,
        mime_type=resolved_mime,
        size=size,
        width=width,
        height=height,
        control_session_id=control_session_id,
        principal_id=principal_id or control_session_id,
        conversation_id=conversation_id,
        created_at=now,
        expires_at=now + max(60, int(expires_in)),
    )


def _is_regular_file(path: Path) -> bool:
    try:
        file_stat = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(file_stat.st_mode) and path.is_file()


def _is_read_allowed_by_policy(path: Path) -> bool:
    try:
        from app.agent.controller_policy import ActionType, resolve_permission

        decision = resolve_permission(ActionType.READ, target_path=str(path))
    except Exception:
        return False
    return not decision.blocked and not decision.requires_access_grant


def _valid_attachment_target(
    raw_path: str | Path,
    *,
    enforce_policy: bool = False,
) -> Path | None:
    try:
        source = Path(raw_path).expanduser()
        if source.is_symlink():
            return None
        target = source.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if target.is_symlink() or not _is_regular_file(target):
        return None
    try:
        if target.stat().st_size > MAX_ATTACHMENT_DOWNLOAD_BYTES:
            return None
    except OSError:
        return None
    if enforce_policy and not _is_read_allowed_by_policy(target):
        return None
    return target


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
    items: Iterable[Any]
    if payload.get("version") == 2 and isinstance(payload.get("items"), list):
        items = payload["items"]
    else:
        items = [
            {"id": attachment_id, "path": raw_path}
            for attachment_id, raw_path in payload.items()
        ]
    for item in items:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "")
        raw_path = item.get("path")
        if not attachment_id:
            continue
        target = _valid_attachment_target(raw_path)
        if target is None:
            continue
        expires_at = float(item.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            continue
        record = _record_from_path(
            attachment_id,
            target,
            control_session_id=str(item.get("control_session_id") or ""),
            principal_id=str(item.get("principal_id") or ""),
            conversation_id=str(item.get("conversation_id") or ""),
            mime_type=str(item.get("mime_type") or ""),
            width=item.get("width") if isinstance(item.get("width"), int) else None,
            height=item.get("height") if isinstance(item.get("height"), int) else None,
        )
        record.created_at = float(item.get("created_at") or record.created_at)
        record.expires_at = expires_at or record.expires_at
        _ATTACHMENT_REGISTRY[str(attachment_id)] = record


def _cleanup_attachment_registry() -> None:
    now = time.time()
    expired = [
        attachment_id
        for attachment_id, record in _ATTACHMENT_REGISTRY.items()
        if record.expires_at <= now or _valid_attachment_target(record.path) is None
    ]
    for attachment_id in expired:
        _ATTACHMENT_REGISTRY.pop(attachment_id, None)
    if len(_ATTACHMENT_REGISTRY) <= MAX_ATTACHMENT_REGISTRY_ITEMS:
        return
    ordered = sorted(_ATTACHMENT_REGISTRY.values(), key=lambda record: record.created_at)
    for record in ordered[: len(_ATTACHMENT_REGISTRY) - MAX_ATTACHMENT_REGISTRY_ITEMS]:
        _ATTACHMENT_REGISTRY.pop(record.id, None)


def _persist_attachment_registry() -> None:
    try:
        _cleanup_attachment_registry()
        _ATTACHMENT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "items": [
                asdict(record)
                for record in sorted(_ATTACHMENT_REGISTRY.values(), key=lambda item: item.created_at)
                if _valid_attachment_target(record.path) is not None
            ],
        }
        tmp_path = _ATTACHMENT_REGISTRY_PATH.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(_ATTACHMENT_REGISTRY_PATH)
    except OSError:
        return


def attachment_registry_stats() -> dict[str, int]:
    _load_attachment_registry()
    _cleanup_attachment_registry()
    return {
        "attachment_registry_items": len(_ATTACHMENT_REGISTRY),
        "attachment_registry_max_items": MAX_ATTACHMENT_REGISTRY_ITEMS,
        "attachment_registry_expiry_seconds": ATTACHMENT_EXPIRY_SECONDS,
    }


def clear_attachment_registry(*, delete_registered_files: bool = False) -> dict[str, int]:
    """Clear response attachment registry and optionally registered runtime files."""
    _load_attachment_registry()
    counts = {
        "attachment_registry_items": len(_ATTACHMENT_REGISTRY),
        "attachment_files_deleted": 0,
    }
    if delete_registered_files:
        runtime_root = RUNTIME_DIR.resolve(strict=False)
        for record in list(_ATTACHMENT_REGISTRY.values()):
            try:
                path = Path(record.path)
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=False)
                if resolved.is_file() and resolved.is_relative_to(runtime_root):
                    resolved.unlink()
                    counts["attachment_files_deleted"] += 1
            except OSError:
                continue
    _ATTACHMENT_REGISTRY.clear()
    try:
        _ATTACHMENT_REGISTRY_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return counts


def register_attachment_path(
    attachment_id: str,
    path: str | Path,
    *,
    control_session_id: str = "",
    principal_id: str = "",
    conversation_id: str = "",
    mime_type: str = "",
    width: int | None = None,
    height: int | None = None,
    expires_in: int = ATTACHMENT_EXPIRY_SECONDS,
    enforce_policy: bool = False,
) -> AttachmentRecord | None:
    _load_attachment_registry()
    target = _valid_attachment_target(path, enforce_policy=enforce_policy)
    if target is None:
        return None
    record = _record_from_path(
        attachment_id,
        target,
        control_session_id=control_session_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        mime_type=mime_type,
        width=width,
        height=height,
        expires_in=expires_in,
    )
    _ATTACHMENT_REGISTRY[str(attachment_id)] = record
    _persist_attachment_registry()
    return record


def _record_matches_scope(
    record: AttachmentRecord,
    *,
    control_session_id: str = "",
    principal_id: str = "",
    conversation_id: str = "",
    allow_internal: bool = False,
) -> bool:
    if allow_internal:
        return True
    if record.control_session_id and record.control_session_id != control_session_id:
        return False
    if record.principal_id and principal_id and record.principal_id != principal_id:
        return False
    if record.principal_id and not principal_id and record.control_session_id != control_session_id:
        return False
    if record.conversation_id and record.conversation_id not in {conversation_id, "pending"}:
        return False
    return True


def resolve_attachment_record(
    attachment_id: str,
    *,
    control_session_id: str = "",
    principal_id: str = "",
    conversation_id: str = "",
    allow_internal: bool = False,
) -> AttachmentRecord | None:
    _load_attachment_registry()
    _cleanup_attachment_registry()
    record = _ATTACHMENT_REGISTRY.get(_attachment_id_from_handle(attachment_id))
    if record is None:
        return None
    path = _valid_attachment_target(record.path)
    if path is None:
        _ATTACHMENT_REGISTRY.pop(record.id, None)
        _persist_attachment_registry()
        return None
    if not _record_matches_scope(
        record,
        control_session_id=control_session_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        allow_internal=allow_internal,
    ):
        return None
    return record


def revalidate_attachment_path(path: str | Path) -> Path | None:
    target = _valid_attachment_target(path, enforce_policy=True)
    if target is None:
        return None
    return target


def resolve_attachment_path(
    attachment_id: str,
    *,
    control_session_id: str = "",
    principal_id: str = "",
    conversation_id: str = "",
    allow_internal: bool = False,
) -> Path | None:
    record = resolve_attachment_record(
        attachment_id,
        control_session_id=control_session_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
        allow_internal=allow_internal,
    )
    return Path(record.path) if record is not None else None


def bind_attachment_to_conversation(
    attachment_id: str,
    *,
    control_session_id: str,
    principal_id: str,
    conversation_id: str,
) -> AttachmentRecord | None:
    record = resolve_attachment_record(
        attachment_id,
        control_session_id=control_session_id,
        principal_id=principal_id,
        conversation_id="pending",
    )
    if record is None:
        return None
    if record.conversation_id in {"", "pending"} and conversation_id:
        record.conversation_id = conversation_id
        _persist_attachment_registry()
    return record


def resolve_attachment_handle_path(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text.startswith(ATTACHMENT_HANDLE_PREFIX):
        return None
    return resolve_attachment_path(
        text,
        control_session_id=current_control_session_id(),
        principal_id=current_principal_id(),
        conversation_id=current_conversation_id(),
    )


def runtime_attachment_from_ref(
    attachment: Any,
    *,
    control_session_id: str,
    principal_id: str,
    conversation_id: str,
) -> dict[str, Any] | None:
    attachment_id = str(getattr(attachment, "id", "") or "").strip()
    if not attachment_id and isinstance(attachment, dict):
        attachment_id = str(attachment.get("id") or "").strip()
    if not attachment_id:
        return None
    record = bind_attachment_to_conversation(
        attachment_id,
        control_session_id=control_session_id,
        principal_id=principal_id,
        conversation_id=conversation_id,
    )
    if record is None:
        record = resolve_attachment_record(
            attachment_id,
            control_session_id=control_session_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
    if record is None:
        return None
    payload = public_attachment_payload(record)
    payload["path"] = record.path
    payload["handle"] = attachment_handle(record.id)
    return payload


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


def _attachment_from_resolved_path(
    path: Path,
    *,
    control_session_id: str | None = None,
    principal_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    if path.suffix.lower() not in _RETURNABLE_EXTENSIONS:
        return None
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    image_dimensions = get_image_dimensions(path) if mime_type.startswith("image/") else None
    if _is_invalid_generated_screenshot(path, image_dimensions):
        _delete_file(path)
        return None
    attachment_id = new_attachment_id()
    record = register_attachment_path(
        attachment_id,
        path,
        control_session_id=current_control_session_id() if control_session_id is None else control_session_id,
        principal_id=current_principal_id() if principal_id is None else principal_id,
        conversation_id=current_conversation_id() if conversation_id is None else conversation_id,
        mime_type=mime_type,
        width=image_dimensions[0] if image_dimensions is not None else None,
        height=image_dimensions[1] if image_dimensions is not None else None,
        enforce_policy=True,
    )
    return public_attachment_payload(record) if record is not None else None


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
    control_session_id: str | None = None,
    principal_id: str | None = None,
    conversation_id: str | None = None,
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
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            return
        attachment = _attachment_from_resolved_path(
            path,
            control_session_id=control_session_id,
            principal_id=principal_id,
            conversation_id=conversation_id,
        )
        if attachment is None:
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
