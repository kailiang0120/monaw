from __future__ import annotations

import importlib.util
import json
from typing import Any

from app.agent.approval_broker import create_ticket
from app.agent.execution_resume import register_executor
from app.agent.settings_store import MCPServerConfig

from .connection import (
    ensure_mcp_manager,
    get_mcp_runtime_diagnostics,
    reconnect_mcp_server as reconnect_runtime_mcp_server,
)
from .registry import build_tool_entries, clear_reflected_tools_for_server, get_reflected_tool_map


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _parse_input(input_str: str) -> dict[str, Any]:
    try:
        parsed = json.loads(input_str) if input_str else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mcp_available() -> bool:
    return importlib.util.find_spec("mcp") is not None


def _iter_servers(settings) -> list[MCPServerConfig]:
    raw_servers = getattr(getattr(settings, "mcp", object()), "servers", []) or []
    servers: list[MCPServerConfig] = []
    for raw in raw_servers:
        cfg = raw if isinstance(raw, MCPServerConfig) else MCPServerConfig.model_validate(raw)
        servers.append(cfg)
    return servers


def _iter_enabled_servers(settings) -> list[MCPServerConfig]:
    if not bool(getattr(getattr(settings, "mcp", object()), "enabled", True)):
        return []
    return [cfg for cfg in _iter_servers(settings) if cfg.enabled]


def _server_entry_predicate(server_name: str):
    def _matches(tool: dict) -> bool:
        bridge = tool.get("mcp_bridge") or {}
        return bridge.get("server_name") == server_name

    return _matches


def _refresh_server_tools(registry, cfg: MCPServerConfig) -> dict[str, Any]:
    if not _mcp_available():
        clear_reflected_tools_for_server(cfg.name)
        registry.replace_where(_server_entry_predicate(cfg.name), [])
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
    registry.replace_where(_server_entry_predicate(cfg.name), entries)
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
    def _control_approval(tool_name: str, action: str, reason: str, args: dict[str, Any]) -> str:
        target = str(args.get("server_name", "") or "all")
        payload = {"input_str": json.dumps(args, ensure_ascii=False, sort_keys=True), "args": args}
        ticket = create_ticket(
            action_type="mcp_control",
            tool_name=tool_name,
            target_app=target,
            risk_level="medium",
            reason=reason,
            action_description=action,
            payload=payload,
        )
        return _json(
            {
                "status": "pending_approval",
                "ticket_id": ticket.id,
                "action": action,
                "reason": reason,
                "tool": tool_name,
                "server_name": args.get("server_name", ""),
            }
        )

    def mcp_status() -> str:
        """Return runtime status for configured and active MCP servers."""
        return _mcp_status(settings)

    def mcp_list_tools(server_name: str = "") -> str:
        mapping = get_reflected_tool_map()
        tools = [
            {"reflected_name": reflected_name, **details}
            for reflected_name, details in sorted(mapping.items())
            if not server_name or details.get("server_name") == server_name
        ]
        return _json({"ok": True, "server_name": server_name, "tools": tools})

    def _raw_mcp_refresh_tools(server_name: str = "") -> str:
        configs = _iter_enabled_servers(settings)
        if server_name:
            configs = [cfg for cfg in configs if cfg.name == server_name]
            if not configs:
                return _json({"ok": False, "status": "error", "error": f"MCP server '{server_name}' not found"})
        results = [_refresh_server_tools(registry, cfg) for cfg in configs]
        return _json({"ok": all(item.get("ok") for item in results), "servers": results})

    def mcp_refresh_tools(server_name: str = "") -> str:
        configs = _iter_enabled_servers(settings)
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

    def _raw_mcp_reconnect_server(server_name: str) -> str:
        status = reconnect_runtime_mcp_server(server_name)
        if status is None:
            return _json({"ok": False, "status": "error", "error": f"MCP server '{server_name}' not found"})
        cfg = next((item for item in _iter_enabled_servers(settings) if item.name == server_name), None)
        refreshed = _refresh_server_tools(registry, cfg) if cfg is not None else {}
        return _json({"ok": True, "status": status, "refresh": refreshed})

    def mcp_reconnect_server(server_name: str) -> str:
        if not any(cfg.name == server_name for cfg in _iter_enabled_servers(settings)):
            return _json({"ok": False, "status": "error", "error": f"MCP server '{server_name}' not found"})
        return _control_approval(
            "mcp_reconnect_server",
            f"Reconnect MCP server {server_name}",
            "mcp_control_restarts_configured_server",
            {"server_name": server_name},
        )

    register_executor(
        "mcp_refresh_tools",
        lambda input_str: _raw_mcp_refresh_tools(server_name=str(_parse_input(input_str).get("server_name", "") or "")),
    )
    register_executor(
        "mcp_reconnect_server",
        lambda input_str: _raw_mcp_reconnect_server(server_name=str(_parse_input(input_str).get("server_name", "") or "")),
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
