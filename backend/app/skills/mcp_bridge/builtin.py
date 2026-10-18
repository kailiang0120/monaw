"""MCP servers Monaw ships as part of a built-in skill.

They are not stored in user settings, are not shown in the MCP settings list,
and run whether or not the user turned on their own MCP connections.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.agent.permission_actions import ActionType
from app.agent.settings_store import MCPServerConfig

CHROME_DEVTOOLS_SERVER = "chrome-devtools"
# Pinned to the version whose option names and tool table below were checked.
CHROME_DEVTOOLS_PACKAGE = "chrome-devtools-mcp@1.11.0"
CHROME_DEVTOOLS_ARGS = [
    "-y",
    CHROME_DEVTOOLS_PACKAGE,
    # Drive the user's own running Chrome profile.
    "--autoConnect",
    "--no-usage-statistics",
    "--no-performance-crux",
    # Removes take_heapsnapshot; the other memory tools also need --memoryDebugging.
    "--no-category-memory",
    "--no-category-performance",
    "--no-category-extensions",
    "--no-file-navigations",
    "--redact-network-headers",
]
CHROME_DEVTOOLS_ENV = {
    "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1",
    "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "1",
}
# Built-in server name -> the skill its tools belong to.
BUILTIN_SERVER_SKILLS = {CHROME_DEVTOOLS_SERVER: "browser-use"}
# Tests set this so loading tools never launches npx.
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


_FILE_WRITING = BuiltinToolPolicy(ActionType.MUTATE, "medium", "builtin_browser_writes_files")

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
        "list_network_requests": _read(),
        "get_network_request": _read(),
        "get_css_styles": _read(),
        "click": _act(ActionType.CLICK),
        "click_at": _act(ActionType.CLICK),
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
        "resize_page": _act(ActionType.LAUNCH_APP, "builtin_browser_navigation"),
        "emulate": _act(ActionType.LAUNCH_APP, "builtin_browser_navigation"),
        "evaluate_script": BuiltinToolPolicy(ActionType.EXEC, "high", "builtin_browser_script_execution"),
        "upload_file": BuiltinToolPolicy(
            ActionType.MUTATE, "high", "builtin_browser_file_upload", always_confirm=True,
        ),
        "lighthouse_audit": _FILE_WRITING,
        "screencast_start": _FILE_WRITING,
        "screencast_stop": _FILE_WRITING,
        "take_heapsnapshot": _FILE_WRITING,
        "performance_start_trace": _FILE_WRITING,
        "performance_stop_trace": _FILE_WRITING,
    },
}

# Calls to these tools have their `url` argument checked against the browser
# URL policy before the server sees them.
BUILTIN_URL_TOOLS: dict[str, frozenset[str]] = {
    CHROME_DEVTOOLS_SERVER: frozenset({"navigate_page", "new_page"}),
}


def builtin_mcp_servers() -> list[MCPServerConfig]:
    if os.environ.get(DISABLE_ENV) == "1":
        return []
    return [
        MCPServerConfig(
            name=CHROME_DEVTOOLS_SERVER,
            command="npx",
            args=list(CHROME_DEVTOOLS_ARGS),
            env=dict(CHROME_DEVTOOLS_ENV),
            startup_timeout_ms=60000,
            call_timeout_ms=90000,
            description="Your Chrome: page snapshots, hover, drag, dialogs, uploads and other browser actions.",
        )
    ]


def is_builtin_server(name: str) -> bool:
    return name in BUILTIN_SERVER_SKILLS


def builtin_tool_policy(server_name: str, tool_name: str) -> BuiltinToolPolicy | None:
    return BUILTIN_TOOL_POLICIES.get(server_name, {}).get(tool_name)


def builtin_url_tool(server_name: str, tool_name: str) -> bool:
    return tool_name in BUILTIN_URL_TOOLS.get(server_name, frozenset())
