from __future__ import annotations

import importlib.util
import json
from typing import Any

from app.agent.action_security import attest_action, authorize_action
from app.agent.action_security.sensitive_data import redact_sensitive
from app.agent.execution_resume import register_executor
from app.agent.settings_store import MCPServerConfig

from .builtin import builtin_mcp_servers, is_builtin_server

from .connection import (
    ensure_mcp_manager,
    get_mcp_runtime_diagnostics,
    validate_mcp_endpoint,
    reconnect_mcp_server as reconnect_runtime_mcp_server,
)
from .registry import (
    _runtime_approval_decision,
    _server_identity,
    _schema_hash,
    build_tool_entries,
    clear_reflected_tools_for_server,
    get_reflected_tool_map,
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(redact_sensitive(payload), ensure_ascii=False)


def _parse_input(input_str: str) -> dict[str, Any]:
    try:
        parsed = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mcp_available() -> bool:
    return importlib.util.find_spec("mcp") is not None


def _iter_user_servers(settings) -> list[MCPServerConfig]:
    raw_servers = getattr(getattr(settings, "mcp", object()), "servers", []) or []
    servers: list[MCPServerConfig] = []
    for raw in raw_servers:
        cfg = raw if isinstance(raw, MCPServerConfig) else MCPServerConfig.model_validate(raw)
        if not is_builtin_server(cfg.name):
            servers.append(cfg)
    return servers


def _iter_servers(settings) -> list[MCPServerConfig]:
    return [*_iter_user_servers(settings), *builtin_mcp_servers(settings)]


def _iter_enabled_servers(settings) -> list[MCPServerConfig]:
    # The user's MCP switch covers their own servers, not built-in ones.
    user_servers: list[MCPServerConfig] = []
    if bool(getattr(getattr(settings, "mcp", object()), "enabled", True)):
        user_servers = [cfg for cfg in _iter_user_servers(settings) if cfg.enabled]
    return [*user_servers, *builtin_mcp_servers(settings)]


def _server_entry_predicate(server_name: str):
    def _matches(tool: dict) -> bool:
        bridge = tool.get("mcp_bridge") or {}
        return bridge.get("server_name") == server_name

    return _matches


def _refresh_server_tools(registry, cfg: MCPServerConfig) -> dict[str, Any]:
    if not _mcp_available():
        clear_reflected_tools_for_server(cfg.name)
        registry.remove_where(_server_entry_predicate(cfg.name))
        return {
            "name": cfg.name,
            "ok": False,
            "error": "mcp package is not installed",
            "reflected_tool_names": [],
        }

    manager = ensure_mcp_manager(cfg)
    if manager.connected:
        refreshed = manager.refresh_tools()
    else:
        refreshed = manager.start()

    entries = build_tool_entries(cfg, manager, manager.tools) if refreshed else []
    if not refreshed:
        clear_reflected_tools_for_server(cfg.name)
        manager.reflected_tool_names = []
    registry.remove_where(_server_entry_predicate(cfg.name))
    registry.extend(entries)
    return {
        "name": cfg.name,
        "ok": bool(refreshed),
        "state": manager.state,
        "connected": manager.connected,
        "tool_count": manager.tool_count,
        "last_error": manager.last_error or None,
        "reflected_tool_names": [entry["name"] for entry in entries],
    }


def _mcp_status(settings) -> str:
    configured = {cfg.name: cfg for cfg in _iter_servers(settings)}
    diagnostics = {item["name"]: item for item in get_mcp_runtime_diagnostics()}
    servers: list[dict[str, Any]] = []
    for name, cfg in configured.items():
        entry = diagnostics.get(name, {})
        servers.append(
            {
                "name": name,
                "enabled": cfg.enabled,
                "configured": True,
                "transport": cfg.transport,
                "description": cfg.description,
                "connected": bool(entry.get("connected", False)),
                "state": entry.get("state", "stopped"),
                "tool_count": int(entry.get("tool_count", 0) or 0),
                "last_error": entry.get("last_error"),
                "unhealthy_reason": entry.get("unhealthy_reason"),
                "reflected_tool_names": entry.get("reflected_tool_names", []),
            }
        )
    for name, entry in diagnostics.items():
        if name in configured:
            continue
        servers.append({**entry, "configured": False, "enabled": False})
    return _json(
        {
            "ok": True,
            "enabled": bool(getattr(getattr(settings, "mcp", object()), "enabled", True)),
            "mcp_package_available": _mcp_available(),
            "server_count": len(servers),
            "servers": servers,
        }
    )


def register_tools(registry, settings) -> None:
    def _control_configs():
        # Reload the backend settings at both approval and dispatch, rather
        # than launching from the runtime's potentially stale tool closure.
        from app.agent import controller_policy
        return _iter_enabled_servers(controller_policy._load_runtime_settings())

    def _control_approval(tool_name: str, action: str, reason: str, args: dict[str, Any]) -> str:
        decision = _runtime_approval_decision(
            {"requires_approval": True, "risk": "medium", "reason": reason}, "launch_mcp_server",
        )
        if decision.get("blocked"):
            return _json({"status": "blocked", "reason": decision["reason"], "reason_code": decision["reason_code"]})
        configs = _control_configs()
        target_name = str(args.get("server_name") or "")
        if target_name:
            configs = [cfg for cfg in configs if cfg.name == target_name]
        # Dispatch the same private snapshot that is hashed for authorization;
        # a second settings read could select a different launch command.
        configs = [cfg.model_copy(deep=True) for cfg in configs]
        try:
            for cfg in configs:
                validate_mcp_endpoint(cfg)
        except ValueError:
            return _json({"status": "blocked", "reason_code": "mcp_destination_not_allowed",
                          "error": "The configured MCP endpoint is outside its destination policy."})
        identities = {cfg.name: _server_identity(cfg) for cfg in configs}
        descriptor = attest_action(tool_name=tool_name, action_kind="mcp_control", parameters=args,
            target={"server_id": target_name or "all", "configurations": identities},
            destination=",".join(cfg.url for cfg in configs if cfg.url), context_version=_schema_hash(identities),
            side_effect="unknown", runtime_facts={"target_verified": bool(configs), "context_verified": True,
                "explicit_tool_trust": True, "adapter_permission_requires_confirmation": decision["requires_approval"]})
        result = authorize_action(descriptor, replay_payload={"args": args})
        if result is not None:
            return _json(result)
        if tool_name == "mcp_refresh_tools":
            return _raw_mcp_refresh_tools(configs)
        return _raw_mcp_reconnect_server(configs[0])

    def mcp_status() -> str:
        """Return runtime status for configured and active MCP servers."""
        return _mcp_status(settings)

    def mcp_list_tools(server_name: str = "") -> str:
        mapping = get_reflected_tool_map()
        tools = [
            {"reflected_name": reflected_name, **{key: details[key] for key in
                ("server_name", "original_tool_name", "annotations", "approval", "schema_hash") if key in details}}
            for reflected_name, details in sorted(mapping.items())
            if not server_name or details.get("server_name") == server_name
        ]
        return _json({"ok": True, "server_name": server_name, "tools": tools})

    def _raw_mcp_refresh_tools(configs: list[MCPServerConfig]) -> str:
        results = [_refresh_server_tools(registry, cfg) for cfg in configs]
        return _json({"ok": all(item.get("ok") for item in results), "servers": results})

    def mcp_refresh_tools(server_name: str = "") -> str:
        configs = _control_configs()
        if server_name:
            configs = [cfg for cfg in configs if cfg.name == server_name]
            if not configs:
                return _json({"ok": False, "status": "error", "error": f"MCP server '{server_name}' not found"})
        if not configs:
            return _json({"ok": True, "servers": []})
        return _control_approval(
            "mcp_refresh_tools",
            f"Refresh MCP tools for {server_name or 'all enabled servers'}",
            "mcp_control_may_start_or_contact_server",
            {"server_name": server_name},
        )

    def _raw_mcp_reconnect_server(cfg: MCPServerConfig) -> str:
        status = reconnect_runtime_mcp_server(cfg.name, expected_config=cfg)
        if status is None:
            return _json({"ok": False, "status": "error", "error": f"MCP server '{cfg.name}' not found"})
        refreshed = _refresh_server_tools(registry, cfg)
        return _json({"ok": True, "status": "ok", "server": status, "refresh": refreshed})

    def mcp_reconnect_server(server_name: str) -> str:
        if not any(cfg.name == server_name for cfg in _control_configs()):
            return _json({"ok": False, "status": "error", "error": f"MCP server '{server_name}' not found"})
        return _control_approval(
            "mcp_reconnect_server",
            f"Reconnect MCP server {server_name}",
            "mcp_control_restarts_configured_server",
            {"server_name": server_name},
        )

    register_executor(
        "mcp_refresh_tools",
        lambda input_str: mcp_refresh_tools(server_name=str(_parse_input(input_str).get("server_name", "") or "")),
    )
    register_executor(
        "mcp_reconnect_server",
        lambda input_str: mcp_reconnect_server(server_name=str(_parse_input(input_str).get("server_name", "") or "")),
    )

    registry.extend(
        [
            {
                "name": "mcp_status",
                "description": "Show local MCP bridge status for configured and active servers.",
                "parameters": {"type": "object", "properties": {}, "required": []},
                "callable": mcp_status,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "mcp_list_tools",
                "description": "List reflected MCP tools and their original server/tool names.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server_name": {
                            "type": "string",
                            "description": "Optional MCP server name to filter by.",
                            "default": "",
                        }
                    },
                    "required": [],
                },
                "callable": mcp_list_tools,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "mcp_refresh_tools",
                "description": "Refresh reflected MCP tools from one or all enabled servers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server_name": {
                            "type": "string",
                            "description": "Optional MCP server name to refresh.",
                            "default": "",
                        }
                    },
                    "required": [],
                },
                "callable": mcp_refresh_tools,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
            {
                "name": "mcp_reconnect_server",
                "description": "Reconnect an MCP server and refresh its reflected tool list.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "server_name": {
                            "type": "string",
                            "description": "Configured MCP server name.",
                        }
                    },
                    "required": ["server_name"],
                },
                "callable": mcp_reconnect_server,
                "domain": "general",
                "execution_mode": "sync_stateless",
                "affinity_group": None,
            },
        ]
    )

    if not _mcp_available():
        return

    for cfg in _iter_enabled_servers(settings):
        _refresh_server_tools(registry, cfg)
