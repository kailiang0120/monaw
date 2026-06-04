from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.agent import runtime
from app.agent.settings_store import AgentSettings, MCPServerConfig
from app.main import app


def test_reset_runtime_cache_calls_mcp_reset(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr("app.skills.mcp_bridge.connection.reset_mcp_runtime", lambda: calls.append("mcp"))
    monkeypatch.setattr(runtime, "_runtimes", {})

    runtime.reset_runtime_cache()

    assert calls == ["mcp"]


def test_reset_runtime_cache_retires_active_runtime_without_shutdown(monkeypatch):
    calls: list[str] = []
    active = runtime.AgentRuntime.__new__(runtime.AgentRuntime)
    active._active_runs = 1
    active._retired = False
    active.shutdown = lambda: calls.append("shutdown")

    monkeypatch.setattr("app.skills.mcp_bridge.connection.reset_mcp_runtime", lambda: None)
    monkeypatch.setattr(runtime, "_runtimes", {"active": active})

    runtime.reset_runtime_cache()

    assert calls == []
    assert active._retired is True
    assert runtime._runtimes == {}


def test_get_mcp_diagnostics_endpoint_returns_status(monkeypatch):
    settings_data = AgentSettings()
    settings_data.mcp.servers = [MCPServerConfig(name="filesystem", enabled=True, description="Files")]

    monkeypatch.setattr(
        "app.api.routes.diagnostics.get_mcp_runtime_diagnostics",
        lambda: [
            {
                "name": "filesystem",
                "enabled": True,
                "transport": "stdio",
                "connected": True,
                "state": "connected",
                "tool_count": 2,
                "last_error": None,
                "unhealthy_reason": None,
                "description": "Files",
                "command": "npx",
                "args": ["-y", "server"],
                "cwd": r"C:\Repo",
                "url": "",
                "resolved_executable": r"C:\nodejs\npx.cmd",
                "startup_phase": "ready",
                "pid": 123,
                "started_at": "2026-04-24T00:00:00+00:00",
                "connected_at": "2026-04-24T00:00:01+00:00",
                "disconnected_at": None,
                "stderr_tail": "",
                "last_call_started_at": None,
                "last_call_duration_ms": 12,
                "failed_call_count": 0,
                "remote_tool_names": ["list_directory"],
                "reflected_tool_names": ["mcp__filesystem__list_directory"],
            }
        ],
    )
    monkeypatch.setattr("app.api.routes.diagnostics.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(
        "app.api.routes.diagnostics.load_agent_settings",
        lambda _settings: settings_data,
    )

    client = TestClient(app)
    response = client.get("/api/diagnostics/mcp")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["name"] == "filesystem"
    assert payload[0]["connected"] is True
    assert payload[0]["state"] == "connected"
    assert payload[0]["startup_phase"] == "ready"
    assert payload[0]["reflected_tool_names"] == ["mcp__filesystem__list_directory"]
    assert payload[0]["feature_enabled"] is True
    assert payload[0]["feature_available"] is False
    assert payload[0]["feature_unavailable_reason"] == "missing_backend_dependency"


def test_reconnect_mcp_endpoint_restarts_server(monkeypatch):
    restarted: list[str] = []
    settings_data = AgentSettings()
    settings_data.mcp.enabled = True

    monkeypatch.setattr(
        "app.api.routes.diagnostics.reconnect_mcp_server",
        lambda name: (
            restarted.append(name),
            {"name": name, "connected": True, "tool_count": 3, "last_error": None, "description": ""},
        )[1],
    )
    monkeypatch.setattr("app.api.routes.diagnostics.load_agent_settings", lambda _settings: settings_data)

    client = TestClient(app)
    response = client.post("/api/diagnostics/mcp/filesystem/reconnect")

    assert response.status_code == 200
    assert restarted == ["filesystem"]
    assert response.json()["connected"] is True


def test_reconnect_mcp_endpoint_rejects_when_feature_disabled(monkeypatch):
    settings_data = AgentSettings()
    settings_data.mcp.enabled = False
    monkeypatch.setattr("app.api.routes.diagnostics.load_agent_settings", lambda _settings: settings_data)

    client = TestClient(app)
    response = client.post("/api/diagnostics/mcp/filesystem/reconnect")

    assert response.status_code == 409


def test_update_settings_restarts_enabled_mcp_servers_after_save(monkeypatch):
    settings_data = AgentSettings()

    saved: list[AgentSettings] = []
    reset_calls: list[dict] = []
    restarted: list[list[str]] = []

    monkeypatch.setattr("app.api.routes.settings.load_agent_settings", lambda _settings: settings_data)
    monkeypatch.setattr(
        "app.api.routes.settings.save_agent_settings",
        lambda updated: (saved.append(updated), updated)[1],
    )
    monkeypatch.setattr(
        "app.api.routes.settings.reset_runtime_cache",
        lambda **kwargs: reset_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "app.api.routes.settings.restart_enabled_mcp_servers",
        lambda updated: restarted.append([server.name for server in updated.mcp.servers]),
        raising=False,
    )

    client = TestClient(app)
    response = client.put(
        "/api/settings",
        json={
            "mcp": {
                "enabled": True,
                "servers": [
                    {
                        "name": "filesystem",
                        "enabled": True,
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", r"C:\Repo"],
                        "env": {},
                        "cwd": "",
                        "url": "",
                        "headers": {},
                        "startup_timeout_ms": 8000,
                        "call_timeout_ms": 30000,
                        "reconnect_on_unhealthy": True,
                        "allow_list": [],
                        "description": "Files",
                    }
                ]
            }
        },
    )

    assert response.status_code == 200
    assert len(saved) == 1
    assert reset_calls == [{"reset_mcp": True, "reset_browser": False}]
    assert restarted == [["filesystem"]]


def test_update_settings_key_sync_noops_when_key_value_is_unchanged(monkeypatch):
    reset_calls: list[dict] = []

    monkeypatch.setattr("app.config.settings.openai_api_key", "same-key")
    monkeypatch.setattr(
        "app.api.routes.settings.reset_runtime_cache",
        lambda **kwargs: reset_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "app.api.routes.settings.restart_enabled_mcp_servers",
        lambda _updated: None,
        raising=False,
    )

    client = TestClient(app)
    response = client.put("/api/settings", json={"openai_api_key": "same-key"})

    assert response.status_code == 200
    assert reset_calls == []


def test_model_options_endpoint_returns_backend_model_catalog():
    client = TestClient(app)
    response = client.get("/api/settings/model-options")

    assert response.status_code == 200
    payload = response.json()
    providers = {item["id"]: item for item in payload["providers"]}
    assert providers["openai"]["models"][0] == "gpt-5.5"
    assert providers["gemini"]["label"] == "Google"
    assert providers["gemini"]["models"][0].startswith("gemini-3")
    assert "gemini-3.1-flash-lite-preview" in payload["vision_fallback_models"]


def test_update_settings_key_change_does_not_reset_browser_runtime(monkeypatch):
    reset_calls: list[dict] = []
    restarted: list[str] = []

    monkeypatch.setattr("app.config.settings.openai_api_key", "old-key")
    monkeypatch.setattr(
        "app.api.routes.settings.reset_runtime_cache",
        lambda **kwargs: reset_calls.append(kwargs),
    )
    monkeypatch.setattr(
        "app.api.routes.settings.restart_enabled_mcp_servers",
        lambda _updated: restarted.append("mcp"),
        raising=False,
    )

    client = TestClient(app)
    response = client.put("/api/settings", json={"openai_api_key": "new-key"})

    assert response.status_code == 200
    assert reset_calls == [{"reset_mcp": False, "reset_browser": False}]
    assert restarted == []


def test_update_settings_returns_saved_permissions_payload(monkeypatch):
    settings_data = AgentSettings()

    monkeypatch.setattr("app.api.routes.settings.load_agent_settings", lambda _settings: settings_data)
    monkeypatch.setattr(
        "app.api.routes.settings.save_agent_settings",
        lambda updated: updated,
    )
    monkeypatch.setattr("app.api.routes.settings.reset_runtime_cache", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.api.routes.settings.restart_enabled_mcp_servers",
        lambda _updated: None,
        raising=False,
    )

    client = TestClient(app)
    response = client.put(
        "/api/settings",
        json={"permissions": {"mode": "full_access", "allow_screen_fallback": False}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["permissions"]["mode"] == "custom"
    assert payload["permissions"]["allow_screen_fallback"] is False


def test_update_settings_clean_preset_patch_uses_preset_payload(monkeypatch):
    settings_data = AgentSettings()

    monkeypatch.setattr("app.api.routes.settings.load_agent_settings", lambda _settings: settings_data)
    monkeypatch.setattr(
        "app.api.routes.settings.save_agent_settings",
        lambda updated: updated,
    )
    monkeypatch.setattr("app.api.routes.settings.reset_runtime_cache", lambda **_kwargs: None)
    monkeypatch.setattr(
        "app.api.routes.settings.restart_enabled_mcp_servers",
        lambda _updated: None,
        raising=False,
    )

    client = TestClient(app)
    response = client.put("/api/settings", json={"permissions": {"mode": "full_access"}})

    assert response.status_code == 200
    payload = response.json()
    assert payload["permissions"]["mode"] == "full_access"
    assert payload["permissions"]["allow_screen_fallback"] is True
