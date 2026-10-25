"""Tool definitions served by the extension server.

These are the browser tool names the permission table, URL guard, stall
guard, prompts and UI key off (``mcp__chrome-devtools__<name>``). Arguments
that write files (``filePath``) or run init scripts are not offered.
"""

from __future__ import annotations

from typing import Any

_PAGE_ID = {"type": "number", "description": "Targets a specific page by ID from list_pages. Defaults to the selected page."}
_UID = {"type": "string", "description": "The uid of an element on the page from the page content snapshot"}
_TIMEOUT = {
    "type": "integer",
    "description": "Maximum wait time in milliseconds. If set to 0, the default timeout will be used.",
}
_INCLUDE_SNAPSHOT = {"type": "boolean", "description": "Whether to include a snapshot in the response. Default is false."}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_pages",
        "description": "Get a list of pages open in the browser.",
        "inputSchema": _schema({}),
        "readOnly": True,
    },
    {
        "name": "select_page",
        "description": "Select a page as a context for future tool calls.",
        "inputSchema": _schema({
            "pageId": {"type": "number", "description": "The ID of the page to select. Call list_pages to get available pages."},
            "bringToFront": {"type": "boolean", "description": "Whether to focus the page and bring it to the top."},
        }, ["pageId"]),
        "readOnly": True,
    },
    {
        "name": "new_page",
        "description": "Open a new tab and load a URL.",
        "inputSchema": _schema({
            "url": {"type": "string", "description": "URL to load in a new page."},
            "background": {
                "type": "boolean",
                "description": "Whether to open the page in the background without bringing it to the front. Default is false (foreground).",
            },
            "timeout": _TIMEOUT,
        }, ["url"]),
    },
    {
        "name": "navigate_page",
        "description": "Go to a URL, or back, forward, or reload.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "type": {
                "type": "string",
                "enum": ["url", "back", "forward", "reload"],
                "description": "Navigate the page by URL, back or forward in history, or reload.",
            },
            "url": {"type": "string", "description": "Target URL (only type=url)"},
            "ignoreCache": {"type": "boolean", "description": "Whether to ignore cache on reload."},
            "handleBeforeUnload": {
                "type": "string",
                "enum": ["accept", "dismiss"],
                "description": "Whether to auto accept or dismiss beforeunload dialogs triggered by this navigation. Default is accept.",
            },
            "timeout": _TIMEOUT,
        }),
    },
    {
        "name": "close_page",
        "description": "Closes the page by its ID. The last open page cannot be closed.",
        "inputSchema": _schema({
            "pageId": {"type": "number", "description": "The ID of the page to close. Call list_pages to list pages."},
        }, ["pageId"]),
    },
    {
        "name": "take_snapshot",
        "description": (
            "Take a text snapshot of the target page based on the a11y tree. The snapshot lists page elements "
            "along with a unique identifier (uid). Always use the latest snapshot. Prefer taking a snapshot over "
            "taking a screenshot."
        ),
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "verbose": {"type": "boolean", "description": "Accepted for compatibility; the snapshot is always the same."},
        }),
        "readOnly": True,
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot of the page or element.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "format": {
                "type": "string",
                "enum": ["png", "jpeg", "webp"],
                "description": 'Type of format to save the screenshot as. Default is "png"',
            },
            "quality": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "Compression quality for JPEG and WebP formats (0-100). Ignored for PNG format.",
            },
            "uid": {
                "type": "string",
                "description": "The uid of an element on the page from the page content snapshot. If omitted, takes a page screenshot.",
            },
            "fullPage": {
                "type": "boolean",
                "description": "If set to true takes a screenshot of the full page instead of the currently visible viewport. Incompatible with uid.",
            },
        }),
        "readOnly": True,
    },
    {
        "name": "wait_for",
        "description": "Wait for the specified text to appear on the selected page.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "text": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Non-empty list of texts. Resolves when any value appears on the page.",
            },
            "timeout": _TIMEOUT,
        }, ["text"]),
        "readOnly": True,
    },
    {
        "name": "click",
        "description": "Clicks on the provided element",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "uid": _UID,
            "dblClick": {"type": "boolean", "description": "Set to true for double clicks. Default is false."},
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["uid"]),
    },
    {
        "name": "hover",
        "description": "Hover over the provided element",
        "inputSchema": _schema({"pageId": _PAGE_ID, "uid": _UID, "includeSnapshot": _INCLUDE_SNAPSHOT}, ["uid"]),
    },
    {
        "name": "drag",
        "description": "Drag an element onto another element",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "from_uid": {"type": "string", "description": "The uid of the element to drag"},
            "to_uid": {"type": "string", "description": "The uid of the element to drop into"},
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["from_uid", "to_uid"]),
    },
    {
        "name": "fill",
        "description": "Type text into a input, text area or select an option from a <select> element.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "uid": _UID,
            "value": {
                "type": "string",
                "description": 'The value to fill in. "true" or "false" for checkboxes and toggles, "true" for radio buttons.',
            },
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["uid", "value"]),
    },
    {
        "name": "fill_form",
        "description": "Fill out multiple form elements at once",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "elements": {
                "type": "array",
                "description": "Elements from snapshot to fill out.",
                "items": {
                    "type": "object",
                    "properties": {
                        "uid": {"type": "string", "description": "The uid of the element to fill out"},
                        "value": {
                            "type": "string",
                            "description": 'Value for the element. "true" or "false" for checkboxes and toggles, "true" for radio buttons.',
                        },
                    },
                    "required": ["uid", "value"],
                    "additionalProperties": False,
                },
            },
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["elements"]),
    },
    {
        "name": "type_text",
        "description": "Type text using keyboard into a previously focused input",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "text": {"type": "string", "description": "The text to type"},
            "submitKey": {"type": "string", "description": 'Optional key to press after typing. E.g., "Enter", "Tab", "Escape"'},
        }, ["text"]),
    },
    {
        "name": "press_key",
        "description": (
            "Press a key or key combination. Use this when other input methods like fill() cannot be used "
            "(e.g., keyboard shortcuts, navigation keys, or special key combinations)."
        ),
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "key": {
                "type": "string",
                "description": 'A key or a combination (e.g., "Enter", "Control+A", "Control++", "Control+Shift+R"). Modifiers: Control, Shift, Alt, Meta',
            },
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["key"]),
    },
    {
        "name": "handle_dialog",
        "description": "If a browser dialog was opened, use this command to handle it",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "action": {"type": "string", "enum": ["accept", "dismiss"], "description": "Whether to dismiss or accept the dialog"},
            "promptText": {"type": "string", "description": "Optional prompt text to enter into the dialog."},
        }, ["action"]),
    },
    {
        "name": "upload_file",
        "description": "Upload one or more files through a provided element.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "uid": {"type": "string", "description": "The uid of the file input element on the page from the page content snapshot"},
            "filePaths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "One or more absolute local file paths to upload.",
            },
            "includeSnapshot": _INCLUDE_SNAPSHOT,
        }, ["uid", "filePaths"]),
    },
    {
        "name": "evaluate_script",
        "description": (
            "Evaluate a JavaScript function inside the currently selected page. Returns the response as JSON, "
            "so returned values have to be JSON-serializable."
        ),
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "function": {
                "type": "string",
                "description": (
                    "A JavaScript function declaration to be executed by the tool in the currently selected page. "
                    "Example without arguments: `() => { return document.title }` or `async () => { return await fetch(\"example.com\") }`. "
                    "Example with arguments: `(el) => { return el.innerText; }`"
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string", "description": "The uid of an element on the page from the page content snapshot"},
                "description": "An optional list of arguments (element uids from the page content snapshot) to pass to the function.",
            },
        }, ["function"]),
    },
    {
        "name": "list_console_messages",
        "description": "List all console messages for the currently selected page since the last navigation.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "pageSize": {"type": "integer", "minimum": 1, "description": "Maximum number of messages to return. When omitted, returns all messages."},
            "pageIdx": {"type": "integer", "minimum": 0, "description": "Page number to return (0-based). When omitted, returns the first page."},
            "types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter messages to only return messages of the specified types (for example log, warning, error). When omitted or empty, returns all messages.",
            },
            "includePreservedMessages": {
                "type": "boolean",
                "description": "Set to true to return the preserved messages over the last 3 navigations.",
            },
        }),
        "readOnly": True,
    },
    {
        "name": "get_console_message",
        "description": "Gets a console message by its ID. You can get all messages by calling list_console_messages.",
        "inputSchema": _schema({
            "pageId": _PAGE_ID,
            "msgid": {"type": "number", "description": "The msgid of a console message on the page from the listed console messages"},
        }, ["msgid"]),
        "readOnly": True,
    },
]

TOOL_NAMES = frozenset(tool["name"] for tool in TOOL_DEFINITIONS)
