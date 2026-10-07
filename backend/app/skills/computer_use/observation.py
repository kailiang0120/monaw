"""Checks for captured desktop evidence and live target identity."""
from __future__ import annotations

import json
from pathlib import Path


def window_changed(expected: dict, actual: dict, *, check_title: bool = True) -> bool:
    keys = ["hwnd", "pid", "process_name", "process_create_time", "dpi"]
    if check_title:
        keys.append("title")
    if any(expected.get(key) != actual.get(key) for key in keys if key in expected):
        return True
    old_rect = expected.get("rect") or {}
    new_rect = actual.get("rect") or {}
    return any(old_rect.get(key) != new_rect.get(key) for key in ("x", "y", "width", "height") if key in old_rect)


def uia_fingerprint(uia: dict) -> str:
    keys = ("name", "automation_id", "control_type", "runtime_id", "bounds", "is_password", "enabled", "value", "selected")
    return json.dumps([{key: element.get(key) for key in keys if key in element}
                       for element in uia.get("elements", [])], sort_keys=True, ensure_ascii=False)


def image_sample(screenshot: dict) -> bytes | None:
    if screenshot.get("status") != "ok" or not screenshot.get("path"):
        return None
    from PIL import Image
    try:
        with Image.open(Path(screenshot["path"])) as image:
            return image.convert("L").resize((64, 64)).tobytes()
    except (OSError, ValueError):
        return None


def image_changed(before: bytes, after: bytes) -> bool:
    # Ignore tiny cursor/caret changes while detecting materially changed pixels.
    return len(before) != len(after) or sum(abs(a - b) for a, b in zip(before, after)) / max(1, len(before)) > 2.0
