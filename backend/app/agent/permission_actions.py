"""Shared action vocabulary and confirmation rules for every tool adapter."""
from __future__ import annotations

import re
from enum import Enum

from app.agent.settings_store import PermissionSettings


class ActionType(str, Enum):
    READ = "read"
    MUTATE = "mutate"
    DELETE = "delete"
    LAUNCH_APP = "launch_app"
    CLICK = "click"
    TYPE = "type"
    EXEC = "exec"
    PROCESS_KILL = "process_kill"


def requires_confirmation(action: ActionType, permissions: PermissionSettings) -> bool:
    if action == ActionType.PROCESS_KILL:
        return permissions.mode != "full_access"
    if action == ActionType.EXEC:
        return permissions.mode in {"default", "auto_review"} or (
            permissions.mode == "custom" and permissions.dangerous_actions_require_confirm
        )
    if action == ActionType.DELETE:
        return permissions.allow_delete and permissions.confirmations.delete
    if permissions.mode == "full_access" or action == ActionType.READ:
        return False
    if permissions.mode in {"default", "auto_review"}:
        return action == ActionType.MUTATE
    switch = {ActionType.MUTATE: "mutate", ActionType.LAUNCH_APP: "launch_app",
              ActionType.CLICK: "click", ActionType.TYPE: "type"}.get(action)
    return bool(switch and getattr(permissions.confirmations, switch))


def external_tool_action(tool_name: str, *, read_only: bool = False) -> ActionType:
    name = tool_name.lower()
    words = set(re.split(r"[^a-z0-9]+", tool_name.lower()))
    if words & {"delete", "remove"}:
        return ActionType.DELETE
    if words & {"exec", "execute", "run", "shell", "command", "evaluate", "eval", "script"}:
        return ActionType.EXEC
    if "click" in words:
        return ActionType.CLICK
    if words & {"type", "fill", "press"}:
        return ActionType.TYPE
    if name in {"new_page", "new_tab", "open_page", "open_url", "open_browser",
                "launch_browser", "launch_app", "launch_mcp_server", "navigate_page"}:
        return ActionType.LAUNCH_APP
    if read_only or words & {"snapshot", "screenshot"}:
        return ActionType.READ
    return ActionType.MUTATE
