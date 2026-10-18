from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from app.agent import controller_policy
from app.agent.approval_broker import create_ticket
from app.agent.controller_policy import resolve_external_tool_permission
from app.agent.permission_actions import ActionType, external_tool_action
from app.agent.execution_resume import register_executor
from app.agent.settings_store import MCPServerConfig
from app.skills.browser_use.url_policy import browser_url_policy

from .builtin import BUILTIN_SERVER_SKILLS, builtin_tool_policy, builtin_url_tool

logger = logging.getLogger(__name__)

MAX_TOOL_NAME_LENGTH = 64
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_ARGUMENT_DEPTH = 12
MAX_SCALAR_LENGTH = 16 * 1024
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")

_BLOCKED_URL_SCHEMES = frozenset({
    "file", "javascript", "vbscript", "data", "blob", "filesystem", "about",
    "chrome", "chrome-extension", "chrome-untrusted", "devtools", "view-source",
})

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
    policy = builtin_tool_policy(cfg.name, tool_name)
    if policy is not None:
        decision = {
            "requires_approval": policy.prompts,
            "risk": policy.risk,
            "reason": policy.reason,
            "action": policy.action.value,
            "read_only": policy.read_only,
        }
        if policy.always_confirm:
            decision["always_confirm"] = True
        return decision

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


def _runtime_approval_decision(decision: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Apply the current desktop profile at call time, after MCP risk classification."""
    explicit_action = decision.get("action")
    if explicit_action:
        action = ActionType(explicit_action)
    else:
        action = external_tool_action(tool_name, read_only=decision["reason"] == "untrusted_read_tool")
    permission = resolve_external_tool_permission(
        action, default_confirmation=decision["requires_approval"], reason=decision["reason"],
    )
    blocked = permission.blocked or not permission.allowed
    requires_approval = permission.requires_confirmation
    reason_code = permission.reason_code
    if decision.get("always_confirm") and not blocked and not requires_approval:
        requires_approval = True
        reason_code = "confirmation_required"
    return {
        **decision,
        "requires_approval": requires_approval,
        "blocked": blocked,
        "reason": permission.reason if permission.blocked or decision["requires_approval"] else decision["reason"],
        "reason_code": reason_code,
    }


def _tool_metadata(decision: dict[str, Any], annotations: Any) -> dict[str, Any]:
    """Harness metadata: read-only tools are observations the repeat guard allows."""
    if "action" in decision:
        if decision.get("read_only"):
            return {"observation": True, "mutates_state": False, "risk_level": "low"}
        metadata: dict[str, Any] = {"mutates_state": True, "risk_level": decision["risk"]}
        if decision["action"] in {ActionType.CLICK.value, ActionType.TYPE.value, ActionType.LAUNCH_APP.value}:
            # The turn loop's page-state-aware stall guard handles repeats
            # instead of the blanket identical-call cap.
            metadata["repeat_safe"] = True
        return metadata
    if (
        _annotation_value(annotations, "readOnlyHint") is True
        and _annotation_value(annotations, "destructiveHint") is not True
        and decision["risk"] != "high"
    ):
        return {"observation": True, "mutates_state": False, "risk_level": decision["risk"]}
    return {}


def _url_guard(server_name: str, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Check built-in navigation URLs with Monaw's browser URL policy before the server sees them.

    Returns the (possibly normalized) arguments and, when blocked, a JSON result for the model.
    """
    if not builtin_url_tool(server_name, tool_name):
        return arguments, None

    def _blocked(reason_code: str, error: str, **details: Any) -> str:
        return json.dumps(
            {"status": "blocked", "reason_code": reason_code, "reason": error, "error": error,
             "server": server_name, "tool": tool_name, **details},
            ensure_ascii=False,
        )

    if arguments.get("initScript"):
        return arguments, _blocked(
            "init_script_not_allowed",
            "navigate_page initScript runs page JavaScript; use evaluate_script, which asks for approval.",
        )
    raw_url = arguments.get("url")
    raw_text = str(raw_url or "").strip()
    if not raw_text and tool_name == "navigate_page":
        # navigate_page back/forward/reload carries no URL.
        return arguments, None
    scheme = urlparse(raw_text).scheme.lower()
    if scheme in _BLOCKED_URL_SCHEMES or (scheme and "://" in raw_text and scheme not in {"http", "https"}):
        return arguments, _blocked(
            "url_scheme_not_allowed",
            "Only http and https URLs can be opened in the browser.",
            url=raw_text,
            scheme=scheme,
        )
    browser_settings = getattr(controller_policy._load_runtime_settings(), "browser", None)
    domains = [str(item) for item in (getattr(browser_settings, "allowed_domains", None) or [])]
    normalized_url, policy_error = browser_url_policy(
        SimpleNamespace(config={"allowed_domains": domains}), raw_text, action=tool_name,
    )
    if policy_error is not None:
        details = {key: value for key, value in policy_error.items() if key not in {"status", "reason_code", "error"}}
        return arguments, _blocked(
            str(policy_error.get("reason_code") or "url_not_allowed"),
            str(policy_error.get("error") or "URL is not allowed."),
            **details,
        )
    return {**arguments, "url": normalized_url}, None


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
    permission = _runtime_approval_decision(current.get("approval", {"requires_approval": True,
        "reason": "untrusted_tool", "risk": "medium"}), tool_name)
    if permission.get("blocked"):
        return json.dumps({"status": "blocked", "reason": permission["reason"],
                           "reason_code": permission["reason_code"]})
    if current.get("schema_hash") != schema_hash:
        return json.dumps(
            {
                "status": "error",
                "error": "MCP tool schema changed after approval.",
                "reason_code": "mcp_schema_changed",
            },
            ensure_ascii=False,
        )
    arguments, url_blocked = _url_guard(str(current.get("server_name") or ""), tool_name, arguments)
    if url_blocked is not None:
        return url_blocked
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
            effective = _runtime_approval_decision(_decision, _tool_name)
            if effective.get("blocked"):
                return json.dumps({"status": "blocked", "reason": effective["reason"],
                                   "reason_code": effective["reason_code"]})
            # Don't ask the user to approve a URL that would be refused anyway.
            _, url_blocked = _url_guard(cfg.name, _tool_name, arguments)
            if url_blocked is not None:
                return url_blocked
            if effective["requires_approval"]:
                return _pending_mcp_approval(
                    reflected_name=_reflected_name,
                    server_name=cfg.name,
                    original_tool_name=_tool_name,
                    arguments=arguments,
                    decision=effective,
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
                "metadata": _tool_metadata(decision, annotations),
                "mcp_bridge": {
                    "server_name": cfg.name,
                    # Built-in servers count as part of the skill that ships them.
                    "skill": BUILTIN_SERVER_SKILLS.get(cfg.name, ""),
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
