"""MCP servers Monaw ships as part of a built-in skill.

They are not stored in user settings, are not shown in the MCP settings list,
and run whether or not the user turned on their own MCP connections.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent.permission_actions import ActionType
from app.agent.settings_store import MCPServerConfig

# The built-in browser server. It keeps the chrome-devtools name (and the tool
# names below) so permissions, the stall guard and the UI stay unchanged; it is
# served by the Monaw Chrome extension (app/skills/browser_use/extension).
CHROME_DEVTOOLS_SERVER = "chrome-devtools"
# Built-in server name -> the skill its tools belong to.
BUILTIN_SERVER_SKILLS = {CHROME_DEVTOOLS_SERVER: "browser-use"}
# Tests set this so loading tools never starts the browser server.
DISABLE_ENV = "MONAW_DISABLE_BUILTIN_MCP"


@dataclass(frozen=True)
class BuiltinToolPolicy:
    """How Monaw gates one built-in MCP tool, instead of guessing from its name."""

    action: ActionType
    risk: str
    reason: str
    read_only: bool = False
    # MCP's default trust prompt. The permission profile still decides whether
    # it shows: READ/CLICK/TYPE/LAUNCH_APP skip it in interactive default mode.
    prompts: bool = True
    # Ask every time, whatever the permission mode.
    always_confirm: bool = False


def _read(reason: str = "builtin_browser_read") -> BuiltinToolPolicy:
    return BuiltinToolPolicy(ActionType.READ, "low", reason, read_only=True, prompts=False)


def _act(action: ActionType, reason: str = "builtin_browser_interaction") -> BuiltinToolPolicy:
    return BuiltinToolPolicy(action, "medium", reason)


# Keyed by the server's original tool name. Tools missing here fall back to the
# generic keyword classification in registry._approval_decision.
BUILTIN_TOOL_POLICIES: dict[str, dict[str, BuiltinToolPolicy]] = {
    CHROME_DEVTOOLS_SERVER: {
        "list_pages": _read(),
        "select_page": _read(),
        "take_snapshot": _read(),
        "take_screenshot": _read(),
        "wait_for": _read(),
        "list_console_messages": _read(),
        "get_console_message": _read(),
        "click": _act(ActionType.CLICK),
        "hover": _act(ActionType.CLICK),
        "drag": _act(ActionType.CLICK),
        "handle_dialog": _act(ActionType.CLICK),
        "fill": _act(ActionType.TYPE),
        "fill_form": _act(ActionType.TYPE),
        "type_text": _act(ActionType.TYPE),
        "press_key": _act(ActionType.TYPE),
        "navigate_page": _act(ActionType.LAUNCH_APP, "builtin_browser_navigation"),
        "new_page": _act(ActionType.LAUNCH_APP, "builtin_browser_navigation"),
        "close_page": _act(ActionType.LAUNCH_APP, "builtin_browser_navigation"),
        "evaluate_script": BuiltinToolPolicy(ActionType.EXEC, "high", "builtin_browser_script_execution"),
        "upload_file": BuiltinToolPolicy(
            ActionType.MUTATE, "high", "builtin_browser_file_upload", always_confirm=True,
        ),
    },
}

# Calls to these tools have their `url` argument checked against the browser
# URL policy before the server sees them.
BUILTIN_URL_TOOLS: dict[str, frozenset[str]] = {
    CHROME_DEVTOOLS_SERVER: frozenset({"navigate_page", "new_page"}),
}


# The extension server lives in the backend package, so it runs with the
# backend's own Python from the backend folder.
EXTENSION_SERVER_MODULE = "app.skills.browser_use.extension"
_BACKEND_DIR = Path(__file__).resolve().parents[3]


def builtin_mcp_servers(settings: Any = None) -> list[MCPServerConfig]:
    if os.environ.get(DISABLE_ENV) == "1":
        return []
    from app.skills.browser_use.extension.common import DEFAULT_PORT, PORT_ENV

    browser = getattr(settings, "browser", None)
    if browser is None:
        from app.agent.settings_store import load_agent_settings

        browser = load_agent_settings().browser
    return [
        MCPServerConfig(
            name=CHROME_DEVTOOLS_SERVER,
            command=sys.executable,
            args=["-m", EXTENSION_SERVER_MODULE],
            cwd=str(_BACKEND_DIR),
            env={PORT_ENV: str(int(getattr(browser, "extension_port", DEFAULT_PORT) or DEFAULT_PORT))},
            startup_timeout_ms=30000,
            call_timeout_ms=90000,
            description="Your Chrome, through the Monaw extension: page snapshots, clicks, typing, dialogs, uploads and screenshots.",
        )
    ]


def is_builtin_server(name: str) -> bool:
    return name in BUILTIN_SERVER_SKILLS


def builtin_tool_policy(server_name: str, tool_name: str) -> BuiltinToolPolicy | None:
    return BUILTIN_TOOL_POLICIES.get(server_name, {}).get(tool_name)


def builtin_url_tool(server_name: str, tool_name: str) -> bool:
    return tool_name in BUILTIN_URL_TOOLS.get(server_name, frozenset())
