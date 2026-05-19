from __future__ import annotations

import asyncio
import json
from pathlib import Path

from PIL import Image

from app.agent import response_attachments as attachments_module
from app.api.routes.files import get_local_file_preview
from app.agent.response_attachments import (
    collect_response_attachments,
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


def test_collect_response_attachments_keeps_previewable_image_dimensions(tmp_path: Path):
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


def test_collect_response_attachments_can_include_generated_tool_screenshots(tmp_path: Path):
    screenshot = tmp_path / "browser_page_ok.png"
    screenshot.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        tool_calls=[
            {"output": json.dumps({"status": "ok", "path": str(screenshot)})}
        ],
        include_generated_tool_screenshots=True,
    )

    assert [attachment["name"] for attachment in result] == ["browser_page_ok.png"]


def test_collect_response_attachments_keeps_explicit_generated_screenshot_paths(tmp_path: Path):
    screenshot = tmp_path / "browser_page_ok.png"
    screenshot.write_bytes(_png_header(536, 320))

    result = collect_response_attachments(
        explicit_paths=[str(screenshot)],
        tool_calls=[
            {"output": json.dumps({"status": "ok", "path": str(screenshot)})}
        ],
    )

    assert [attachment["name"] for attachment in result] == ["browser_page_ok.png"]


def test_collect_response_attachments_keeps_tool_output_deliverable_images(tmp_path: Path):
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


def test_file_preview_endpoint_returns_thumbnail(monkeypatch, tmp_path: Path):
    registry_path = tmp_path / "attachment_registry.json"
    target = tmp_path / "large.png"
    Image.new("RGB", (1200, 900), color="white").save(target)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(attachments_module, "_ATTACHMENT_REGISTRY_LOADED", False)
    attachments_module._ATTACHMENT_REGISTRY.clear()
    register_attachment_path("image-1", target)

    response = asyncio.run(get_local_file_preview("image-1"))

    assert response.media_type == "image/png"
    assert len(response.body) > 0
