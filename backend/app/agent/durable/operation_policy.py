"""Conservative policy for classifying durable tool operations."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.durable.models import EffectClass


# These names are a deliberately small allowlist. Descriptor metadata and model
# supplied arguments never promote a tool to read-only: unknown tools may hide
# external effects behind native handlers.
READ_ONLY_TOOL_NAMES = frozenset(
    {
        "web_search",
        "browser_fetch",
        "calculator",
        "datetime",
        "memory_search",
        "fs_stat",
        "fs_list",
        "fs_tree",
        "fs_read",
        "fs_search",
        "fs_hash",
        "file_reader",
        "file_stat",
        "file_exists",
        "file_list",
        "file_tree",
        "file_glob",
        "file_read",
        "file_search",
        "file_hash",
        "read_file",
        "exec_poll",
        "computer_functions_list_apps",
        "computer_functions_get_window",
        "computer_functions_get_window_state",
        "computer_functions_list_processes",
        "computer_functions_diagnose",
    }
)


# These native single-action handlers create their ticket before the guarded
# action. Batch handlers are excluded unless their result proves that the
# pending item was the first one, so an approval never silently replays earlier
# successful items.
_TRUSTED_SINGLE_PREFLIGHT_TOOLS = frozenset(
    {
        "fs_write",
        "fs_append",
        "fs_patch",
        "fs_mkdir",
        "fs_copy",
        "fs_move",
        "fs_rename",
        "fs_delete",
        "file_write",
        "file_append",
        "file_patch",
        "create_folder",
        "copy",
        "move",
        "rename",
        "delete",
        "exec",
        "exec_start",
        "exec_write_stdin",
        "computer_functions_act",
        "computer_functions_clipboard",
        "computer_functions_kill_process",
        "explorer_open",
        "explorer_reveal",
    }
)
_BATCH_PREFLIGHT_TOOLS = frozenset({"fs_copy_many", "fs_move_many", "fs_delete_many"})

_FILE_KINDS = {
    "fs_move": "file_move",
    "file_move": "file_move",
    "move": "file_move",
    "fs_rename": "file_move",
    "rename": "file_move",
    "fs_write": "file_write",
    "fs_append": "file_write",
    "fs_patch": "file_write",
    "file_write": "file_write",
    "file_append": "file_write",
    "file_patch": "file_write",
    "fs_delete": "file_delete",
    "delete": "file_delete",
    "fs_copy": "file_copy",
    "copy": "file_copy",
    "fs_mkdir": "file_mkdir",
    "create_folder": "file_mkdir",
}


@dataclass(frozen=True)
class OperationPolicy:
    operation_kind: str
    effect_class: EffectClass
    journal_tool_name: str


def classify_tool(tool_name: str, *, trusted_builtin: bool = False) -> OperationPolicy:
    """Return a bounded journal classification for the resolved tool name."""
    name = str(tool_name or "unknown").strip().lower()
    effect: EffectClass = (
        "read_only" if trusted_builtin and name in READ_ONLY_TOOL_NAMES else "opaque_effect"
    )

    operation_kind = "other" if name in READ_ONLY_TOOL_NAMES else _FILE_KINDS.get(name)
    if operation_kind is None:
        if name == "undo" or name.startswith("undo_"):
            operation_kind = "undo"
        elif name.startswith("exec") or name == "shell_command":
            operation_kind = "shell_command"
        elif name.startswith(("browser", "devtools_", "mcp__chrome")):
            operation_kind = "browser_action"
        elif name.startswith("mcp__") or name == "mcp_call":
            operation_kind = "mcp_call"
        elif name.startswith(("fs_", "file_")):
            operation_kind = "file_mutation"
        else:
            operation_kind = "tool_call"

    journal_name = {
        "file_move": "move_file",
        "file_write": "write_file",
        "file_delete": "delete_file",
        "file_copy": "copy_file",
        "file_mkdir": "create_directory",
        "file_mutation": "file_mutation",
        "undo": "undo",
        "shell_command": "shell_command",
        "browser_action": "browser_action",
        "mcp_call": "mcp_call",
        "tool_call": "tool_call",
        "other": "tool_call",
    }[operation_kind]
    return OperationPolicy(operation_kind, effect, journal_name)


def is_trusted_preflight(
    tool_name: str, output: object, *, trusted_builtin: bool = False
) -> bool:
    """Whether a pending result proves a native preflight stopped before effects.

    This is intentionally based on a known native handler and a narrow output
    shape. Arbitrary tools and batches with prior successful items cannot be
    replayed automatically after a pending gate.
    """
    import json

    try:
        payload = json.loads(output) if isinstance(output, str) else output
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("status") not in {
        "pending_approval",
        "pending_access_grant",
    }:
        return False
    if not isinstance(payload.get("ticket_id"), str) or not payload["ticket_id"].strip():
        return False

    name = str(tool_name or "").strip().lower()
    if not trusted_builtin:
        return False
    if name in _TRUSTED_SINGLE_PREFLIGHT_TOOLS:
        return True
    if name not in _BATCH_PREFLIGHT_TOOLS:
        return False

    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return False
    only = results[0]
    return only.get("status") in {"pending_approval", "pending_access_grant"}
