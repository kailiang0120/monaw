import json
from types import SimpleNamespace

from app.agent.approval_broker import approve_ticket
from app.agent.execution_resume import resume_approved_ticket
from app.agent.settings_store import AgentSettings, MCPServerConfig
from app.agent.tool_registry import ToolRegistry
from app.skills.mcp_bridge.registry import build_tool_entries, clear_all_reflected_tools
from app.skills.mcp_bridge import tools


def test_mcp_public_introspection_omits_private_config_and_control_replay_rechecks_it(monkeypatch):
    from app.agent import controller_policy
    settings = AgentSettings()
    cfg = MCPServerConfig(name="private", command="fixture", env={"API_KEY": "unlabelled-private-config-value"})
    settings.mcp.servers = [cfg]
    settings.permissions.mode = "custom"
    settings.permissions.confirmations.launch_app = True
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)
    monkeypatch.setattr(tools, "_mcp_available", lambda: False)
    registry = ToolRegistry()
    tools.register_tools(registry, settings)
    clear_all_reflected_tools()
    build_tool_entries(cfg, SimpleNamespace(call_tool_sync=lambda *_: "{}"), [SimpleNamespace(name="read", inputSchema={"type":"object"})])
    public = registry.get_tool("mcp_list_tools")["callable"]()
    assert "unlabelled-private-config-value" not in public
    assert "manager_id" not in public and '"config"' not in public
    assert json.loads(public)["tools"][0]["original_tool_name"] == "read"
    pending = json.loads(registry.get_tool("mcp_refresh_tools")["callable"](server_name="private"))
    assert pending["status"] == "pending_approval"
    cfg.command = "replacement-command"
    monkeypatch.setattr(tools, "_refresh_server_tools", lambda *_: (_ for _ in ()).throw(AssertionError("changed server launched")))
    result = resume_approved_ticket(approve_ticket(pending["ticket_id"]))
    assert result.status.value == "failed"
    assert "action_approval_invalidated" in result.execution_result
    clear_all_reflected_tools()


def test_reconnect_dispatches_approved_snapshot_instead_of_stale_manager_or_later_settings(monkeypatch):
    from app.agent import controller_policy
    from app.skills.mcp_bridge import connection
    settings = AgentSettings()
    cfg = MCPServerConfig(name="server", command="approved-command")
    settings.mcp.servers = [cfg]
    settings.permissions.mode = "custom"
    settings.permissions.confirmations.launch_app = True
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)
    monkeypatch.setattr(tools, "_mcp_available", lambda: False)
    calls = []
    class Manager:
        def __init__(self, config):
            self.cfg = config.model_copy(deep=True)
        def close(self):
            calls.append(("close", self.cfg.command))
        def restart(self):
            calls.append(("start", self.cfg.command))
            return True
        def status(self):
            return {"name": self.cfg.name, "connected": True}
    stale = Manager(cfg.model_copy(update={"command": "stale-command"}))
    monkeypatch.setattr(connection, "_ACTIVE_MANAGERS", {cfg.name: stale})
    monkeypatch.setattr(connection, "ServerManager", Manager)
    monkeypatch.setattr("app.agent.runtime.reset_runtime_cache", lambda **_kw: None)
    monkeypatch.setattr(tools, "_refresh_server_tools", lambda _r, config: {"ok": True, "command": config.command})
    registry = ToolRegistry()
    tools.register_tools(registry, settings)
    authorize = tools.authorize_action
    def race_after_authorization(*args, **kwargs):
        result = authorize(*args, **kwargs)
        if result is None:
            cfg.command = "changed-after-authorization"
        return result
    monkeypatch.setattr(tools, "authorize_action", race_after_authorization)
    pending = json.loads(registry.get_tool("mcp_reconnect_server")["callable"](server_name=cfg.name))
    result = resume_approved_ticket(approve_ticket(pending["ticket_id"]))
    assert result.status.value == "applied", result.execution_result
    assert calls == [("close", "stale-command"), ("start", "approved-command")]
    assert json.loads(result.execution_result)["refresh"]["command"] == "approved-command"
    clear_all_reflected_tools()


def test_disallowed_http_endpoint_never_opens_transport_or_control_dispatch(monkeypatch):
    import asyncio
    import pytest
    from app.agent import controller_policy
    from app.skills.mcp_bridge import connection
    cfg = MCPServerConfig(name="http-server", transport="streamable_http", url="https://blocked.example/mcp",
                          allowed_destinations=["https://approved.example"])
    contacts = []
    def transport(*_args, **_kwargs):
        contacts.append("contacted")
        raise AssertionError("disallowed transport opened")
    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", transport)
    with pytest.raises(ValueError, match="mcp_destination_not_allowed"):
        asyncio.run(connection.ServerManager(cfg)._serve())
    settings = AgentSettings()
    settings.mcp.servers = [cfg]
    settings.permissions.mode = "full_access"
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)
    monkeypatch.setattr(tools, "_mcp_available", lambda: False)
    registry = ToolRegistry()
    tools.register_tools(registry, settings)
    monkeypatch.setattr(tools, "_refresh_server_tools", transport)
    result = json.loads(registry.get_tool("mcp_refresh_tools")["callable"](server_name=cfg.name))
    assert result["reason_code"] == "mcp_destination_not_allowed"
    assert contacts == []
