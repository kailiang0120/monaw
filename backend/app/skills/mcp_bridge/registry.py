from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any

from app.agent.approval_broker import create_ticket
from app.agent.execution_resume import register_executor
from app.agent.settings_store import MCPServerConfig

logger = logging.getLogger(__name__)

MAX_TOOL_NAME_LENGTH = 64
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_ARGUMENT_DEPTH = 12
MAX_SCALAR_LENGTH = 16 * 1024
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_REFLECTED_TOOL_MAP: dict[str, dict[str, Any]] = {}
_REFLECTED_TOOL_LOCK = threading.RLock()

_READ_PREFIXES = (
    "get",
    "list",
    "read",
    "search",
    "query",
    "find",
    "inspect",
    "status",
    "describe",
)
_DESTRUCTIVE_KEYWORDS = {
    "write",
    "create",
    "update",
    "delete",
    "remove",
    "move",
    "rename",
    "edit",
    "patch",
    "insert",
    "upload",
    "send",
    "post",
    "put",
    "execute",
    "exec",
    "run",
    "shell",
    "command",
    "launch",
    "click",
    "type",
    "submit",
}
_OPEN_WORLD_KEYWORDS = {
    "browser",
    "web",
    "url",
    "http",
    "request",
    "navigate",
    "fetch",
    "email",
    "message",
}


def _annotation_value(annotations: Any, key: str) -> Any:
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        return annotations.get(key)
    return getattr(annotations, key, None)


def _tool_annotations(tool: Any) -> Any:
    return getattr(tool, "annotations", None) or getattr(tool, "annotation", None)


def _annotations_payload(annotations: Any) -> Any:
    if annotations is None or isinstance(annotations, (str, int, float, bool)):
        return annotations
    if isinstance(annotations, dict):
        return dict(annotations)
    if hasattr(annotations, "model_dump"):
        return annotations.model_dump()
    if hasattr(annotations, "__dict__"):
        return dict(vars(annotations))
    return str(annotations)


def _sanitize_segment(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "tool"


def reflected_tool_name(server_name: str, tool_name: str, *, used: set[str] | None = None) -> str:
    prefix = f"mcp__{_sanitize_segment(server_name)}__"
    base = _sanitize_segment(tool_name)
    digest = hashlib.sha1(f"{server_name}:{tool_name}".encode("utf-8")).hexdigest()[:8]

    max_base = max(1, MAX_TOOL_NAME_LENGTH - len(prefix))
    candidate = f"{prefix}{base[:max_base]}"
    if len(candidate) > MAX_TOOL_NAME_LENGTH:
        max_base = max(1, MAX_TOOL_NAME_LENGTH - len(prefix) - len(digest) - 1)
        candidate = f"{prefix}{base[:max_base]}_{digest}"

    if used is None:
        return candidate

    if candidate not in used:
        used.add(candidate)
        return candidate

    suffix = 2
    while True:
        suffix_text = f"__{suffix}"
        max_base = max(1, MAX_TOOL_NAME_LENGTH - len(prefix) - len(suffix_text))
        deduped = f"{prefix}{base[:max_base]}{suffix_text}"
        if deduped not in used:
            used.add(deduped)
            return deduped
        suffix += 1


def _schema_hash(schema: Any) -> str:
    try:
        payload = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        payload = json.dumps(str(schema), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_json_value(value: Any, *, depth: int = 0) -> str:
    if depth > MAX_ARGUMENT_DEPTH:
        return "mcp_argument_depth_exceeded"
    if isinstance(value, dict):
        for key, item in value.items():
            if len(str(key)) > MAX_SCALAR_LENGTH:
                return "mcp_argument_scalar_too_large"
            error = _validate_json_value(item, depth=depth + 1)
            if error:
                return error
    elif isinstance(value, list):
        for item in value:
            error = _validate_json_value(item, depth=depth + 1)
            if error:
                return error
    elif isinstance(value, str) and len(value) > MAX_SCALAR_LENGTH:
        return "mcp_argument_scalar_too_large"
    return ""


def _validate_arguments(arguments: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(arguments, ensure_ascii=False)
    except TypeError:
        return "mcp_arguments_not_json_serializable"
    if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
        return "mcp_argument_size_exceeded"
    return _validate_json_value(arguments)


def _approval_decision(cfg: MCPServerConfig, tool_name: str, description: str, annotations: Any) -> dict[str, Any]:
    haystack = f"{tool_name} {description}".lower()
    destructive = any(keyword in haystack for keyword in _DESTRUCTIVE_KEYWORDS)
    open_world = any(keyword in haystack for keyword in _OPEN_WORLD_KEYWORDS)

    annotation_destructive = _annotation_value(annotations, "destructiveHint")
    annotation_open_world = _annotation_value(annotations, "openWorldHint")
    annotation_read_only = _annotation_value(annotations, "readOnlyHint")

    if annotation_destructive is True:
        destructive = True
    if annotation_open_world is True:
        open_world = True

    read_prefix = tool_name.lower().startswith(_READ_PREFIXES)
    read_only = bool(annotation_read_only is True or read_prefix)
    if destructive or open_world:
        read_only = False

    configured_risk = cfg.tool_risk_overrides.get(tool_name)
    if configured_risk:
        risk = configured_risk
    elif destructive:
        risk = "high"
    elif open_world:
        risk = "medium"
    elif read_only:
        risk = "low"
    else:
        risk = "medium"

    if tool_name in set(cfg.trusted_tools or []):
        return {"requires_approval": False, "risk": risk, "reason": "explicitly_trusted_tool"}
    if destructive:
        return {"requires_approval": True, "risk": risk, "reason": "destructive_or_mutating_tool"}
    if open_world:
        return {"requires_approval": True, "risk": risk, "reason": "open_world_tool"}
    if read_only:
        return {"requires_approval": True, "risk": risk, "reason": "untrusted_read_tool"}
    return {"requires_approval": True, "risk": risk, "reason": "unknown_tool_risk"}


def _pending_mcp_approval(
    *,
    reflected_name: str,
    server_name: str,
    original_tool_name: str,
    arguments: dict[str, Any],
    decision: dict[str, Any],
    schema_hash: str,
) -> str:
    validation_error = _validate_arguments(arguments)
    if validation_error:
        return json.dumps({"status": "error", "error": validation_error, "reason_code": validation_error})
    action = f"Run MCP tool {server_name}/{original_tool_name}"
    payload = {
        "input_str": json.dumps(
            {"args": arguments, "schema_hash": schema_hash},
            ensure_ascii=False,
            sort_keys=True,
        ),
        "args": arguments,
        "schema_hash": schema_hash,
        "server": server_name,
        "tool": original_tool_name,
        "reflected_tool": reflected_name,
    }
    ticket = create_ticket(
        action_type="mcp_tool",
        tool_name=reflected_name,
        target_app=server_name,
        risk_level=decision["risk"],
        reason=decision["reason"],
        action_description=action,
        payload=payload,
    )
    return json.dumps(
        {
            "status": "pending_approval",
            "ticket_id": ticket.id,
            "action": action,
            "reason": decision["reason"],
            "server": server_name,
            "tool": original_tool_name,
            "reflected_tool": reflected_name,
            "schema_hash": schema_hash,
        },
        ensure_ascii=False,
    )


def _call_mcp_tool(manager, reflected_name: str, tool_name: str, arguments: dict[str, Any], schema_hash: str) -> str:
    validation_error = _validate_arguments(arguments)
    if validation_error:
        return json.dumps({"status": "error", "error": validation_error, "reason_code": validation_error})
    current = get_reflected_tool_map().get(reflected_name, {})
    if current.get("schema_hash") != schema_hash:
        return json.dumps(
            {
                "status": "error",
                "error": "MCP tool schema changed after approval.",
                "reason_code": "mcp_schema_changed",
            },
            ensure_ascii=False,
        )
    return manager.call_tool_sync(tool_name, arguments)


def get_reflected_tool_map() -> dict[str, dict[str, Any]]:
    with _REFLECTED_TOOL_LOCK:
        return {name: dict(mapping) for name, mapping in _REFLECTED_TOOL_MAP.items()}


def clear_reflected_tools_for_server(server_name: str) -> None:
    with _REFLECTED_TOOL_LOCK:
        for reflected_name, mapping in list(_REFLECTED_TOOL_MAP.items()):
            if mapping.get("server_name") == server_name:
                _REFLECTED_TOOL_MAP.pop(reflected_name, None)


def clear_all_reflected_tools() -> None:
    with _REFLECTED_TOOL_LOCK:
        _REFLECTED_TOOL_MAP.clear()


def build_tool_entries(cfg: MCPServerConfig, manager, mcp_tools: list[Any]) -> list[dict]:
    entries: list[dict] = []
    seen_names: set[str] = set()
    mappings: dict[str, dict[str, Any]] = {}

    for tool in mcp_tools:
        tool_name = str(getattr(tool, "name", "")).strip()
        if not tool_name:
            continue
        if cfg.allow_list and tool_name not in cfg.allow_list:
            continue

        reflected_name = reflected_tool_name(cfg.name, tool_name, used=seen_names)
        schema = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
        description = str(getattr(tool, "description", "") or "").strip()
        annotations = _tool_annotations(tool)
        annotations_payload = _annotations_payload(annotations)
        schema_digest = _schema_hash(schema)
        decision = _approval_decision(cfg, tool_name, description, annotations)

        def _raw_executor(
            input_str: str,
            _tool_name: str = tool_name,
            _reflected_name: str = reflected_name,
        ) -> str:
            try:
                parsed = json.loads(input_str) if input_str else {}
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict) and "args" in parsed:
                arguments = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
                approved_schema_hash = str(parsed.get("schema_hash") or "")
            elif isinstance(parsed, dict):
                arguments = parsed
                approved_schema_hash = ""
            else:
                arguments = {}
                approved_schema_hash = ""
            return _call_mcp_tool(manager, _reflected_name, _tool_name, arguments, approved_schema_hash)

        register_executor(reflected_name, _raw_executor)

        def _callable(
            _tool_name: str = tool_name,
            _reflected_name: str = reflected_name,
            _decision: dict[str, Any] = decision,
            _schema_hash: str = schema_digest,
            **arguments,
        ):
            if _decision["requires_approval"]:
                return _pending_mcp_approval(
                    reflected_name=_reflected_name,
                    server_name=cfg.name,
                    original_tool_name=_tool_name,
                    arguments=arguments,
                    decision=_decision,
                    schema_hash=_schema_hash,
                )
            return _call_mcp_tool(manager, _reflected_name, _tool_name, arguments, _schema_hash)

        entries.append(
            {
                "name": reflected_name,
                "description": description or f"MCP tool '{tool_name}' from server '{cfg.name}'.",
                "parameters": schema,
                "callable": _callable,
                "domain": "general",
                "execution_mode": "sync_thread_affine",
                "affinity_group": f"mcp:{cfg.name}",
                "mcp_bridge": {
                    "server_name": cfg.name,
                    "original_tool_name": tool_name,
                    "annotations": annotations_payload,
                    "approval": decision,
                    "schema_hash": schema_digest,
                },
            }
        )
        mappings[reflected_name] = {
            "server_name": cfg.name,
            "original_tool_name": tool_name,
            "annotations": annotations_payload,
            "approval": decision,
            "schema_hash": schema_digest,
        }

    with _REFLECTED_TOOL_LOCK:
        clear_reflected_tools_for_server(cfg.name)
        _REFLECTED_TOOL_MAP.update(mappings)
    manager.reflected_tool_names = [entry["name"] for entry in entries]
    return entries
