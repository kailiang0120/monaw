from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.agent import runtime
from app.agent.settings_store import AgentSettings, MCPServerConfig
from app.main import app


def _patch_browser_discovery(monkeypatch, *, chrome="C:/Program Files/Google/Chrome/Application/chrome.exe", mcp=None):
    module = "app.api.routes.diagnostics"
    monkeypatch.setattr(f"{module}.find_chrome_executable", lambda: chrome)
    monkeypatch.setattr(f"{module}.get_mcp_runtime_diagnostics", lambda: list(mcp or []))


def test_browser_status_reports_chrome_extension_and_server_state(monkeypatch):
    settings_data = AgentSettings()
    monkeypatch.setattr("app.api.routes.diagnostics.load_agent_settings", lambda _settings: settings_data)
    _patch_browser_discovery(monkeypatch, mcp=[
        {"name": "filesystem", "connected": False, "state": "failed"},
        {
            "name": "chrome-devtools",
            "connected": True,
            "state": "connected",
            "startup_phase": "ready",
            "tool_count": 26,
            "last_error": None,
            "unhealthy_reason": None,
            "connected_at": "2026-10-18T00:00:00Z",
        },
    ])

    response = TestClient(app).get("/api/diagnostics/browser-use")

    assert response.status_code == 200
    payload = response.json()
    assert payload["chrome_found"] is True
    assert payload["chrome_executable"] == ""
    assert payload["extension"]["installed"] is False
    assert payload["extension"]["connected"] is False
    assert payload["devtools"] == {
        "server": "chrome-devtools",
        "running": True,
        "connected": True,
        "state": "connected",
        "startup_phase": "ready",
        "tool_count": 26,
        "last_error": "",
        "unhealthy_reason": "",
        "connected_at": "2026-10-18T00:00:00Z",
    }
    assert payload["last_error"] == ""
    for removed in (
        "session_active", "headless", "dom_inspection_engine", "downloads_dir", "traces_dir",
        # Chrome remote debugging.
        "available_system_profiles", "system_profile_directory", "default_profile_directory",
        "connection", "connection_setting",
    ):
        assert removed not in payload


def test_browser_status_redacts_paths_in_devtools_errors_and_handles_missing_chrome(monkeypatch):
    _patch_browser_discovery(monkeypatch, chrome="", mcp=[
        {"name": "chrome-devtools", "connected": False, "state": "failed",
         "last_error": r"spawn failed at C:\Users\someone\AppData\python.exe", "unhealthy_reason": None},
    ])

    payload = TestClient(app).get("/api/diagnostics/browser-use").json()

    assert payload["chrome_found"] is False
    assert payload["devtools"]["running"] is True
    assert payload["devtools"]["state"] == "failed"
    assert "someone" not in payload["devtools"]["last_error"]
    assert payload["last_error"] == payload["devtools"]["last_error"]


def test_browser_status_before_devtools_server_starts(monkeypatch):
    _patch_browser_discovery(monkeypatch)

    payload = TestClient(app).get("/api/diagnostics/browser-use").json()

    assert payload["devtools"]["running"] is False
    assert payload["devtools"]["state"] == "stopped"
    assert payload["devtools"]["connected"] is False


def test_browser_reset_reconnects_builtin_devtools_server(monkeypatch):
    reconnects: list[str] = []
    _patch_browser_discovery(monkeypatch)
    monkeypatch.setattr(
        "app.api.routes.diagnostics.reconnect_mcp_server",
        lambda name: reconnects.append(name) or {"name": name},
    )

    response = TestClient(app).post("/api/diagnostics/browser-use/reset")

    assert response.status_code == 200
    assert reconnects == ["chrome-devtools"]
    assert response.json()["chrome_found"] is True


def test_diagnostics_summary_browser_section_reports_devtools_state(monkeypatch):
    _patch_browser_discovery(monkeypatch, mcp=[
        {"name": "chrome-devtools", "connected": False, "state": "unhealthy", "unhealthy_reason": "Chrome closed"},
    ])

    response = TestClient(app).get("/api/diagnostics/summary")

    assert response.status_code == 200
    assert response.json()["browser"] == {
        "chrome_found": True,
        "devtools_state": "unhealthy",
        "devtools_connected": False,
        "last_error": "Chrome closed",
    }


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
                "args": ["-y", "server", "--api-key", "secret"],
                "cwd": r"C:\Repo",
                "url": "https://example.test/mcp?token=secret&safe=1",
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
                "env": {"MCP_TOKEN": "secret"},
                "headers": {"Authorization": "Bearer secret"},
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
    assert payload[0]["command"] == "npx"
    assert payload[0]["args"] == ["-y", "server", "--api-key", "<redacted>"]
    assert payload[0]["cwd"] == r"C:\Repo"
    assert payload[0]["resolved_executable"] == r"C:\nodejs\npx.cmd"
    assert "secret" not in str(payload[0]["url"])
    assert "secret" not in str(payload[0]["args"])
    assert payload[0]["pid"] == 123
    assert payload[0]["stderr_tail"] == ""
    assert "env" not in payload[0]
    assert "headers" not in payload[0]
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


def test_observability_detailed_exports_require_support_mode(monkeypatch, tmp_path):
    from app.agent.observability import recorder as recorder_module
    from app.agent.observability.recorder import ObservabilityRecorder

    recorder = ObservabilityRecorder(root=tmp_path / "obs")
    run_id = recorder.start_run(
        conversation_id="conv-support",
        user_message="support prompt",
        model="gpt-test",
        provider="openai",
    )
    recorder.finish_run(run_id=run_id, status="complete", final_output="support output")
    monkeypatch.setattr(recorder_module, "_RECORDER", recorder)

    client = TestClient(app)
    denied_log = client.get("/api/observability/logs/backend")
    denied_export = client.post(f"/api/observability/runs/{run_id}/export-debug-bundle")
    enabled = client.post("/api/observability/support-mode?duration_seconds=60")
    allowed_export = client.post(f"/api/observability/runs/{run_id}/export-debug-bundle")

    assert denied_log.status_code == 403
    assert denied_export.status_code == 403
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert allowed_export.status_code == 200
    assert allowed_export.json()["run_id"] == run_id


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
    assert reset_calls == [{"reset_mcp": True}]
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
    assert providers["openai"]["label"] == "OpenAI API"
    assert providers["openai"]["models"] == ["gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"]
    assert providers["codex"]["label"] == "OpenAI account"
    assert providers["codex"]["models"] == providers["openai"]["models"]
    assert providers["gemini"]["label"] == "Google"
    assert providers["gemini"]["models"] == ["gemini-pro-latest", "gemini-flash-latest", "gemini-flash-lite-latest"]
    assert "deepseek" not in providers
    assert "vision_fallback_models" not in payload


def test_update_settings_key_change_does_not_restart_mcp_servers(monkeypatch):
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
    assert reset_calls == [{"reset_mcp": False}]
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


def test_browser_extension_install_copies_pairs_and_restarts_the_browser_server(monkeypatch, tmp_path):
    import json

    import app.skills.browser_use.extension.common as extension_common

    source = tmp_path / "bundled"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setenv(extension_common.SOURCE_ENV, str(source))
    settings_data = AgentSettings()
    monkeypatch.setattr("app.api.routes.diagnostics.load_agent_settings", lambda _settings: settings_data)
    _patch_browser_discovery(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("app.api.routes.diagnostics._open_folder", lambda folder: opened.append(folder) or True)
    monkeypatch.setattr("app.api.routes.diagnostics._open_extensions_page", lambda: True)
    restarts: list[bool] = []
    monkeypatch.setattr("app.api.routes.diagnostics._restart_browser_server", lambda: restarts.append(True))

    before = TestClient(app).get("/api/diagnostics/browser-use").json()
    assert before["extension"]["installed"] is False

    response = TestClient(app).post("/api/diagnostics/browser-use/extension/install")

    assert response.status_code == 200
    payload = response.json()
    assert payload["folder"] == str(extension_common.installed_extension_dir())
    assert opened == [payload["folder"]]
    assert payload["extensions_page_opened"] is True
    assert restarts == [True]
    assert payload["status"]["extension"]["installed"] is True
    assert payload["status"]["extension"]["paired"] is True
    assert payload["status"]["extension"]["connected"] is False
