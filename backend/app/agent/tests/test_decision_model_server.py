import asyncio
import subprocess

from app.agent import decision_model_server as server_module
from app.agent.decision_model_server import ManagedLayaServer, managed_laya_executable
from app.agent.settings_store import DecisionModelSettings


class FakeProcess:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.exit_code = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.exit_code = 0
        return 0


def _install(monkeypatch, tmp_path, *, healthy=False):
    executable = tmp_path / "laya-serve.exe"
    executable.write_text("", encoding="utf-8")
    started = []

    def fake_popen(args, **kwargs):
        process = FakeProcess(args, **kwargs)
        started.append(process)
        return process

    async def fake_healthy(endpoint):
        return healthy

    monkeypatch.setattr(server_module, "managed_laya_executable", lambda: executable)
    monkeypatch.setattr(server_module, "LAYA_DIR", tmp_path)
    monkeypatch.setattr(server_module, "LAYA_LOG_PATH", tmp_path / "logs" / "laya-serve.log")
    monkeypatch.setattr(server_module, "_healthy", fake_healthy)
    monkeypatch.setattr(server_module.subprocess, "Popen", fake_popen)
    return executable, started


def test_executable_lookup_honors_the_disable_switch(monkeypatch, tmp_path):
    scripts = tmp_path / "laya" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "laya-serve.exe").write_text("", encoding="utf-8")
    (tmp_path / "laya" / ".venv" / "bin").mkdir()
    (tmp_path / "laya" / ".venv" / "bin" / "laya-serve").write_text("", encoding="utf-8")
    monkeypatch.setattr(server_module, "LAYA_DIR", tmp_path / "laya")

    monkeypatch.delenv(server_module.DISABLE_ENV, raising=False)
    assert managed_laya_executable() is not None
    monkeypatch.setenv(server_module.DISABLE_ENV, "1")
    assert managed_laya_executable() is None


def test_starts_hidden_laya_serve_on_the_configured_port_and_stops_it(monkeypatch, tmp_path):
    executable, started = _install(monkeypatch, tmp_path)
    server = ManagedLayaServer()

    status = asyncio.run(server.start(DecisionModelSettings(), api_key="secret"))

    assert status["state"] == "starting"
    assert status["pid"] == 4242
    [process] = started
    assert process.args == [str(executable)]
    env = process.kwargs["env"]
    assert env["LAYA_HOST"] == "127.0.0.1"
    assert env["LAYA_PORT"] == "8421"
    assert env["LAYA_MODELS"] == "english"
    assert env["USE_TF"] == "0"
    assert env["LAYA_API_KEY"] == "secret"
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        assert process.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW

    asyncio.run(server.stop())
    assert process.terminated is True
    assert server.status == {"state": "stopped", "running": False}


def test_reuses_a_server_that_is_already_healthy(monkeypatch, tmp_path):
    _executable, started = _install(monkeypatch, tmp_path, healthy=True)

    status = asyncio.run(ManagedLayaServer().start(DecisionModelSettings()))

    assert status["state"] == "already_running"
    assert started == []


def test_does_nothing_when_disabled_unmanaged_remote_or_not_installed(monkeypatch, tmp_path):
    _executable, started = _install(monkeypatch, tmp_path)
    server = ManagedLayaServer()

    assert asyncio.run(server.start(DecisionModelSettings(enabled=False)))["state"] == "disabled"
    assert asyncio.run(server.start(DecisionModelSettings(managed=False)))["state"] == "disabled"
    remote = DecisionModelSettings(endpoint="http://laya.internal:8000")
    assert asyncio.run(server.start(remote))["state"] == "external_endpoint"
    monkeypatch.setattr(server_module, "managed_laya_executable", lambda: None)
    assert asyncio.run(server.start(DecisionModelSettings()))["state"] == "not_installed"
    assert started == []
    asyncio.run(server.stop())


def test_start_failures_are_reported_not_raised(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path)

    def broken_popen(*args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(server_module.subprocess, "Popen", broken_popen)
    status = asyncio.run(ManagedLayaServer().start(DecisionModelSettings()))

    assert status["state"] == "error"
    assert "access denied" in status["error"]


def test_describe_reports_each_state(monkeypatch, tmp_path):
    _executable, _started = _install(monkeypatch, tmp_path)
    server = ManagedLayaServer()
    config = DecisionModelSettings()

    assert asyncio.run(server.describe(DecisionModelSettings(enabled=False)))["state"] == "off"
    assert asyncio.run(server.describe(config))["state"] == "unreachable"

    asyncio.run(server.start(config))
    assert asyncio.run(server.describe(config))["state"] == "starting"

    async def healthy(endpoint):
        return True

    monkeypatch.setattr(server_module, "_healthy", healthy)
    ready = asyncio.run(server.describe(config))
    assert ready["state"] == "ready"
    assert ready["installed"] is True
    assert ready["endpoint"] == "http://127.0.0.1:8421"

    monkeypatch.setattr(server_module, "managed_laya_executable", lambda: None)
    assert asyncio.run(server.describe(config))["state"] == "not_installed"


def test_describe_explains_a_crashed_server(monkeypatch, tmp_path):
    _executable, started = _install(monkeypatch, tmp_path)
    server = ManagedLayaServer()
    asyncio.run(server.start(DecisionModelSettings()))
    started[0].exit_code = 3

    status = asyncio.run(server.describe(DecisionModelSettings()))

    assert status["state"] == "unreachable"
    assert "exited with code 3" in status["error"]


def test_restart_applies_new_settings(monkeypatch, tmp_path):
    _executable, started = _install(monkeypatch, tmp_path)
    server = ManagedLayaServer()
    asyncio.run(server.start(DecisionModelSettings()))

    status = asyncio.run(server.restart(DecisionModelSettings(enabled=False)))

    assert started[0].terminated is True
    assert status["state"] == "disabled"


def test_status_route_combines_server_state_and_recent_decisions(monkeypatch, tmp_path):
    import json
    import sqlite3
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.agent.settings_store import AgentSettings
    from app.main import app

    db = tmp_path / "observability.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE observability_events (event_type TEXT, metadata_json TEXT, created_at TEXT)")
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany("INSERT INTO observability_events VALUES (?, ?, ?)", [
        ("tool_selection_shadow", json.dumps({"status": "ok", "covered": True, "top_domain": "exec"}), now),
        ("tool_selection_shadow", json.dumps({"status": "error", "covered": None}), "2026-01-01T00:00:00+00:00"),
        ("llm_call_started", "{}", now),
    ])
    conn.commit()
    conn.close()

    class ReadyServer(ManagedLayaServer):
        async def describe(self, config):
            return {"state": "ready", "enabled": True, "managed": True, "endpoint": config.endpoint,
                    "installed": True, "error": ""}

    monkeypatch.setattr("app.api.routes.decision_model.load_agent_settings", lambda _settings: AgentSettings())
    monkeypatch.setattr("app.api.routes.decision_model.get_observability_recorder", lambda: SimpleNamespace(db_path=db))
    monkeypatch.setattr(app.state, "decision_model_server", ReadyServer(), raising=False)

    response = TestClient(app).get("/api/decision-model/status")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ready"
    assert body["decisions_24h"] == 1
    assert body["covered_24h"] == 1
    assert body["scored_24h"] == 1
    assert body["last_top_domain"] == "exec"


def test_saving_decision_model_settings_restarts_the_server(monkeypatch):
    from fastapi.testclient import TestClient

    from app.agent.settings_store import AgentSettings
    from app.main import app

    restarts = []

    class RecordingServer(ManagedLayaServer):
        async def restart(self, config, *, api_key=""):
            restarts.append(config.enabled)
            return {"state": "disabled", "running": False}

    monkeypatch.setattr("app.api.routes.settings.load_agent_settings", lambda _settings: AgentSettings())
    monkeypatch.setattr("app.api.routes.settings.save_agent_settings", lambda updated: updated)
    monkeypatch.setattr("app.api.routes.settings.reset_runtime_cache", lambda **kwargs: None)
    monkeypatch.setattr(app.state, "decision_model_server", RecordingServer(), raising=False)

    client = TestClient(app)
    response = client.put("/api/settings", json={"decision_model": {"enabled": False}})
    unchanged = client.put("/api/settings", json={"decision_model": {"enabled": True}})

    assert response.status_code == 200
    assert unchanged.status_code == 200
    assert restarts == [False]
