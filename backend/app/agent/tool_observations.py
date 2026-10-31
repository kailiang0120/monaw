"""Read tool results: errors, image attachments, repeat keys and short summaries."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from app.agent.output_workspace import configured_screenshots_dir
from app.agent.response_attachments import get_image_dimensions, screenshot_dimension_problem

RECOVERY_RESULT_LIMIT = 1400


# Reflected names of the built-in browser server (mcp_bridge/builtin.py), served by the Monaw extension.
DEVTOOLS_PREFIX = "mcp__chrome-devtools__"
_DEVTOOLS_SNAPSHOT_TOOL = f"{DEVTOOLS_PREFIX}take_snapshot"
_DEVTOOLS_LIST_PAGES_TOOL = f"{DEVTOOLS_PREFIX}list_pages"
_DEVTOOLS_MUTATING_TOOLS = {
    f"{DEVTOOLS_PREFIX}{name}"
    for name in (
        "click", "fill", "fill_form", "type_text", "press_key", "hover", "drag",
        "navigate_page", "new_page", "handle_dialog", "upload_file",
        # Also exempt from the blanket identical-call cap (registry repeat_safe),
        # so this page-state-aware guard must cover them.
        "close_page",
    )
}
_DEVTOOLS_OBSERVATION_TOOLS = {
    f"{DEVTOOLS_PREFIX}{name}" for name in ("take_snapshot", "take_screenshot", "list_pages", "wait_for")
}
DEVTOOLS_STEP_DESCRIPTIONS = {
    "take_snapshot": "Inspect the current browser page",
    "list_pages": "Inspect open browser tabs",
    "select_page": "Switch to the relevant browser tab",
    "take_screenshot": "Capture a screenshot of the browser page",
    "new_page": "Open the requested browser page in a new tab",
    "close_page": "Close the browser tab",
    "click": "Click the identified browser element",
    "hover": "Hover over the identified browser element",
    "drag": "Drag the identified browser element",
    "fill": "Fill the identified browser field",
    "type_text": "Type into the focused browser field",
    "fill_form": "Fill the identified browser form fields",
    "press_key": "Press a key in the browser page",
    "handle_dialog": "Handle the browser dialog",
    "wait_for": "Wait for the expected text on the browser page",
    "upload_file": "Upload a file through the browser page",
    "evaluate_script": "Run a script in the browser page",
}


def is_devtools_tool(tool_name: str) -> bool:
    return tool_name.startswith(DEVTOOLS_PREFIX)


def _compact_for_recovery(value: object, *, limit: int = RECOVERY_RESULT_LIMIT) -> str:
    if value is None:
        return "No previous result is available."
    if isinstance(value, str):
        text = value.strip()
    else:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if not text:
        return "No previous result is available."
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


def build_repeat_recovery_prompt(
    *,
    reason_code: str,
    tool_name: str,
    arguments: dict,
    last_result: object = None,
) -> str:
    arguments_text = _compact_for_recovery(arguments, limit=900)
    result_text = _compact_for_recovery(last_result)
    if reason_code == "stalled_repeat_detected":
        observations = f"`{_DEVTOOLS_SNAPSHOT_TOOL}`, `{_DEVTOOLS_LIST_PAGES_TOOL}`, "
        next_steps = (
            "For the next step, use a browser observation or finish the turn: "
            f"{observations}"
            "or answer the user directly. After inspection, only act again if the target or arguments change."
        )
    elif reason_code == "repeated_blocked_tool_result":
        next_steps = (
            "Do not call the same tool with the same arguments again in this turn. "
            "Ask for permission or access changes if needed, choose a different permitted action, "
            "or answer the user with the blocker."
        )
    else:
        next_steps = (
            "Do not call this exact tool with these exact arguments again in this turn. "
            "Use the prior result, inspect current state, choose a different action, "
            "or answer the user directly."
        )
    return (
        "Loop recovery checkpoint: the last tool request repeated a blocked or stalled action.\n\n"
        f"Reason: `{reason_code}`\n"
        f"Repeated tool: `{tool_name}`\n"
        f"Repeated arguments:\n{arguments_text}\n\n"
        f"Last available result:\n{result_text}\n\n"
        f"{next_steps}"
    )


def is_browser_repeat_guarded(tool_name: str, arguments: dict) -> bool:
    return tool_name in _DEVTOOLS_MUTATING_TOOLS


def is_browser_observation_action(tool_name: str, arguments: dict) -> bool:
    return tool_name in _DEVTOOLS_OBSERVATION_TOOLS


def parse_tool_json(tool_output: str) -> dict:
    try:
        data = json.loads(tool_output) if isinstance(tool_output, str) else tool_output
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def tool_error_code(tool_output: str) -> str:
    payload = parse_tool_json(tool_output)
    for key in ("code", "reason_code", "error_code"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return "tool_error"


_MAX_MCP_IMAGE_ATTACHMENTS = 4


def _extract_mcp_image_attachments(tool_output: str) -> list[dict]:
    """Attach images the MCP bridge saved to the screenshots folder (never arbitrary paths)."""
    payload = parse_tool_json(tool_output)
    images = payload.get("images") if isinstance(payload.get("images"), list) else []
    if not images:
        return []
    try:
        screenshots_dir = configured_screenshots_dir().resolve()
    except OSError:
        return []
    attachments: list[dict] = []
    for image in images[:_MAX_MCP_IMAGE_ATTACHMENTS]:
        if not isinstance(image, dict):
            continue
        raw_path = str(image.get("path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path.parent != screenshots_dir or not path.is_file():
            continue
        dimensions = get_image_dimensions(path)
        if dimensions is not None and screenshot_dimension_problem(*dimensions):
            continue
        mime_type = str(image.get("mime_type") or "") or mimetypes.guess_type(str(path))[0] or "image/png"
        attachments.append({"path": str(path), "mime_type": mime_type})
    return attachments


def extract_image_attachments(tool_name: str, tool_output: str) -> list[dict]:
    if tool_name.startswith("mcp__"):
        return _extract_mcp_image_attachments(tool_output)
    if tool_name != "computer_functions_get_window_state":
        return []
    payload = parse_tool_json(tool_output)
    screenshot = payload.get("screenshot") if isinstance(payload.get("screenshot"), dict) else {}
    path = str(
        payload.get("screenshot_path")
        or payload.get("path")
        or screenshot.get("screenshot_path")
        or screenshot.get("path")
        or ""
    ).strip()
    if not path:
        return []
    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    return [{"path": path, "mime_type": mime_type}]


def attachment_image_payloads(attachments: list[dict] | None) -> list[dict]:
    payloads: list[dict] = []
    for attachment in attachments or []:
        mime_type = str(attachment.get("mime_type") or "").strip()
        path = str(attachment.get("path") or "").strip()
        if not path:
            continue
        if not mime_type:
            mime_type = mimetypes.guess_type(path)[0] or ""
        if mime_type.startswith("image/"):
            payloads.append({"path": path, "mime_type": mime_type})
    return payloads


def _browser_state_fingerprint(tool_output: str) -> str:
    """Describe the page state a DevTools result reports (its text content)."""
    payload = parse_tool_json(tool_output)
    stable = {
        "is_error": bool(payload.get("is_error")),
        # DevTools results describe the page only in text (page list, snapshot).
        "mcp_text": _mcp_text_content(payload),
    }
    return json.dumps(stable, sort_keys=True, ensure_ascii=False)


def _mcp_text_content(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()


def browser_repeat_key(tool_name: str, arguments: dict, tool_output: str) -> str:
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "state": _browser_state_fingerprint(tool_output),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def blocked_tool_key(tool_name: str, arguments: dict, tool_output: str) -> str | None:
    payload = parse_tool_json(tool_output)
    if str(payload.get("status") or "").lower() != "blocked":
        return None
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
            "reason_code": payload.get("reason_code") or payload.get("code") or "",
            "reason": payload.get("reason") or payload.get("error") or "",
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def compact_tool_observation(tool_name: str, tool_output: str) -> str:
    payload = parse_tool_json(tool_output)
    if not payload:
        return "Tool completed."
    if str(payload.get("status") or "").lower() == "error":
        return tool_output
    parts: list[str] = []
    page = payload.get("page") if isinstance(payload.get("page"), dict) else {}
    if page:
        page_label = page.get("title") or page.get("url")
        if page_label:
            parts.append(f"page={page_label}")
    if is_devtools_tool(tool_name):
        if payload.get("is_error"):
            parts.append("error")
        first_line = next((line.strip() for line in _mcp_text_content(payload).splitlines() if line.strip()), "")
        if first_line:
            parts.append(first_line[:160])
        images = payload.get("images") if isinstance(payload.get("images"), list) else []
        parts.extend(
            f"screenshot={image.get('path')}" for image in images if isinstance(image, dict) and image.get("path")
        )
    return "; ".join(parts) or "Tool completed."
