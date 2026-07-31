from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.agent import response_attachments as attachments_module
from app.api.routes import files as files_module
from app.api.routes.files import get_local_file, get_local_file_preview
from app.agent.response_attachments import (
    collect_response_attachments,
    public_attachment_payload,
    register_attachment_path,
    resolve_attachment_path,
)


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_collect_response_attachments_filters_and_deletes_tiny_generated_screenshot(tmp_path: Path):
    screenshot = tmp_path / "browser_snapshot_bad.png"
    screenshot.write_bytes(_png_header(536, 1))

    result = collect_response_attachments(content=f"Screenshot: {screenshot}")

    assert result == []
    assert not screenshot.exists()


def test_collect_response_attachments_keeps_previewable_image_dimensions(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(attachments_module, "_is_read_allowed_by_policy", lambda _path: True)
    image = tmp_path / "browser_snapshot_ok.png"
    image.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(content=f"Screenshot: {image}")

    assert len(result) == 1
    assert result[0]["name"] == "browser_snapshot_ok.png"
    assert result[0]["mime_type"] == "image/png"
    assert result[0]["width"] == 536
    assert result[0]["height"] == 320


def test_collect_response_attachments_skips_generated_tool_screenshots_by_default(tmp_path: Path):
    screenshot = tmp_path / "browser_snapshot_ok.png"
    screenshot.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        tool_calls=[
            {
                "output": json.dumps(
                    {"status": "ok", "screenshot_path": str(screenshot)}
                )
            }
        ],
    )

    assert result == []
    assert screenshot.exists()


def test_collect_response_attachments_can_include_generated_tool_screenshots(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(attachments_module, "_is_read_allowed_by_policy", lambda _path: True)
    screenshot = tmp_path / "browser_page_ok.png"
    screenshot.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        tool_calls=[
            {"output": json.dumps({"status": "ok", "path": str(screenshot)})}
        ],
        include_generated_tool_screenshots=True,
    )

    assert [attachment["name"] for attachment in result] == ["browser_page_ok.png"]


def test_collect_response_attachments_keeps_explicit_generated_screenshot_paths(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(attachments_module, "_is_read_allowed_by_policy", lambda _path: True)
    screenshot = tmp_path / "browser_page_ok.png"
    screenshot.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        explicit_paths=[str(screenshot)],
        tool_calls=[
            {"output": json.dumps({"status": "ok", "path": str(screenshot)})}
        ],
    )

    assert [attachment["name"] for attachment in result] == ["browser_page_ok.png"]


def test_collect_response_attachments_keeps_tool_output_deliverable_images(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(attachments_module, "_is_read_allowed_by_policy", lambda _path: True)
    image = tmp_path / "result image.png"
    image.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        tool_calls=[
            {"output": json.dumps({"status": "ok", "path": str(image)})}
        ],
    )

    assert [attachment["name"] for attachment in result] == ["result image.png"]


def test_attachment_registry_persists_registered_paths(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "result.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()

    register_attachment_path("attachment-1", target)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)

    assert resolve_attachment_path("attachment-1") == target.resolve(strict=False)


def test_attachment_registry_rejects_directories_and_oversized_files(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    directory = tmp_path / "directory"
    directory.mkdir()
    large = tmp_path / "large.txt"
    large.write_text("abcd", encoding="utf-8")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    monkeypatch.setattr(attachments_module, "MAX_ATTACHMENT_DOWNLOAD_BYTES", 3)
    attachments_module._ATTACHMENT_REGISTRY.clear()

    assert register_attachment_path("dir-1", directory) is None
    assert register_attachment_path("large-1", large) is None
    assert attachments_module._ATTACHMENT_REGISTRY == {}


def test_attachment_registry_rejects_symlinks(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "target.txt"
    target.write_text("ok", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is not available in this environment.")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()

    assert register_attachment_path("link-1", link) is None
    assert attachments_module._ATTACHMENT_REGISTRY == {}


def test_attachment_registry_cleanup_removes_expired_and_bounds_size(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    third = tmp_path / "third.txt"
    expired = tmp_path / "expired.txt"
    for path in (first, second, third, expired):
        path.write_text(path.name, encoding="utf-8")

    register_attachment_path("first", first)
    register_attachment_path("second", second)
    register_attachment_path("third", third)
    expired_record = register_attachment_path("expired", expired)
    assert expired_record is not None
    expired_record.expires_at = 1
    monkeypatch.setattr(attachments_module, "MAX_ATTACHMENT_REGISTRY_ITEMS", 2)

    attachments_module._persist_attachment_registry()

    assert "expired" not in attachments_module._ATTACHMENT_REGISTRY
    assert len(attachments_module._ATTACHMENT_REGISTRY) == 2
    assert "first" not in attachments_module._ATTACHMENT_REGISTRY


def test_attachment_registry_scopes_records_by_session_and_conversation(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "result.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()

    record = register_attachment_path(
        "attachment-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )

    assert record is not None
    assert public_attachment_payload(record)["path"] == "attachment://attachment-1"
    assert resolve_attachment_path(
        "attachment-1",
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    ) == target.resolve(strict=False)
    assert resolve_attachment_path(
        "attachment-1",
        control_session_id="session-b",
        principal_id="session-b",
        conversation_id="conv-a",
    ) is None
    assert resolve_attachment_path(
        "attachment-1",
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-b",
    ) is None


def test_file_preview_endpoint_returns_thumbnail(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "large.png"
    Image.new("RGB", (1200, 900), color="white").save(target)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path(
        "image-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(control_session=SimpleNamespace(session_id="session-a"))
    )
    monkeypatch.setattr(files_module, "revalidate_attachment_path", lambda path: Path(path))

    response = asyncio.run(get_local_file_preview(request, "image-1", conversation_id="conv-a"))

    assert response.media_type == "image/png"
    assert len(response.body) > 0


def test_file_preview_endpoint_rejects_other_conversation(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "large.png"
    Image.new("RGB", (1200, 900), color="white").save(target)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path(
        "image-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(control_session=SimpleNamespace(session_id="session-a"))
    )
    monkeypatch.setattr(files_module, "revalidate_attachment_path", lambda path: Path(path))

    with pytest.raises(Exception) as exc_info:
        asyncio.run(get_local_file_preview(request, "image-1", conversation_id="conv-b"))

    assert getattr(exc_info.value, "status_code", None) == 404


def test_file_preview_endpoint_returns_safe_error_for_decode_failure(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "large.png"
    Image.new("RGB", (1200, 900), color="white").save(target)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path(
        "image-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(control_session=SimpleNamespace(session_id="session-a"))
    )
    monkeypatch.setattr(files_module, "revalidate_attachment_path", lambda path: Path(path))

    def fail_preview(_path: str) -> bytes:
        raise ValueError("too large")

    monkeypatch.setattr(files_module, "_render_preview_png", fail_preview)
    monkeypatch.setattr(files_module, "_preview_executor", lambda: None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(get_local_file_preview(request, "image-1", conversation_id="conv-a"))

    assert getattr(exc_info.value, "status_code", None) == 422
    assert "too large" not in str(exc_info.value)


def test_file_endpoint_revalidates_before_serving(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "safe.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path(
        "file-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(control_session=SimpleNamespace(session_id="session-a"))
    )
    monkeypatch.setattr(files_module, "revalidate_attachment_path", lambda _path: None)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(get_local_file(request, "file-1", conversation_id="conv-a"))

    assert getattr(exc_info.value, "status_code", None) == 404


def test_file_endpoint_enforces_download_size_limit(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "safe.txt"
    target.write_text("abcd", encoding="utf-8")
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    monkeypatch.setattr(files_module, "MAX_ATTACHMENT_DOWNLOAD_BYTES", 3)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path(
        "file-1",
        target,
        control_session_id="session-a",
        principal_id="session-a",
        conversation_id="conv-a",
    )
    request = SimpleNamespace(
        state=SimpleNamespace(control_session=SimpleNamespace(session_id="session-a"))
    )
    monkeypatch.setattr(files_module, "revalidate_attachment_path", lambda path: Path(path))

    with pytest.raises(Exception) as exc_info:
        asyncio.run(get_local_file(request, "file-1", conversation_id="conv-a"))

    assert getattr(exc_info.value, "status_code", None) == 413
