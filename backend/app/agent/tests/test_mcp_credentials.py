import asyncio
import hashlib
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent import settings_store as store
from app.agent.observability.redaction import json_dumps
from app.main import app
from app.security import mcp_credentials as secrets
from app.security.control_plane import ALL_SCOPES, mint_control_token
from app.skills.mcp_bridge import connection

PREFIX = "/api/settings/mcp-credentials"
SENTINEL = "fixture-custom-private-value-893782"


def auth():
    return {"Authorization": "Bearer " + mint_control_token(os.environ["MONAW_CONTROL_SECRET"], scopes=["mcp:credentials"])}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(connection, "_ACTIVE_MANAGERS", {})
    current = store.AgentSettings()
    current.mcp.servers = [store.MCPServerConfig(name="one", enabled=False, command=sys.executable,
        env={"API_KEY": SENTINEL, "REGION": "test-region"})]
    store.save_agent_settings(current)
    client = TestClient(app)
    return client, current.mcp.servers[0], tmp_path / "settings.json"


def selection(client, server, key="API_KEY"):
    return {"binding": {"server_id": server.server_id, "transport": server.transport,
                        "target": "env" if server.transport == "stdio" else "headers", "key": key},
            "expected_version": client.get(PREFIX + "/inventory", headers=auth()).json()["settings_version"]}


def migrate(client, server, key="API_KEY"):
    selected = selection(client, server, key)
    prepared = client.post(PREFIX + "/prepare", json=selected, headers=auth())
    assert prepared.status_code == 200
    response = client.post(PREFIX + "/commit", json={**selected, "legacy_digest": prepared.json()["legacy_digest"]}, headers=auth())
    assert response.status_code == 200
    return secrets.CredentialBinding(**selected["binding"])


@pytest.mark.parametrize("method,path", [("GET", "/inventory"), ("POST", "/prepare"), ("POST", "/commit"), ("POST", "/hydrate"), ("POST", "/revoke")])
def test_renderer_cannot_access_vault_protocol(monkeypatch, method, path):
    monkeypatch.setattr("app.api.routes.mcp_credentials.load_agent_settings", lambda *_: pytest.fail("denied before settings read"))
    assert "mcp:credentials" not in ALL_SCOPES
    response = TestClient(app).request(method, PREFIX + path, json={} if method == "POST" else None)
    assert response.status_code == 403


def test_migration_removes_plaintext_and_keeps_scoped_reference(configured):
    client, server, path = configured
    assert SENTINEL not in client.get("/api/settings").text
    assert SENTINEL not in client.get(PREFIX + "/inventory", headers=auth()).text
    binding = migrate(client, server)
    assert SENTINEL not in path.read_text()
    assert binding.reference in path.read_text()
    payload = client.get("/api/settings").json()
    assert payload["mcp"]["servers"][0]["env"] == {"REGION": "test-region"}
    assert payload["mcp"]["servers"][0]["credential_refs"]["env"]["API_KEY"] == binding.reference
    assert client.post(PREFIX + "/hydrate", json={"values": [{"binding": binding.model_dump(), "value": SENTINEL}]}, headers=auth()).status_code == 200
    restored = store.load_agent_settings().mcp.servers[0]
    assert secrets.resolved_fields(restored)[0]["API_KEY"] == SENTINEL
    assert SENTINEL not in restored.model_dump_json()
    assert SENTINEL not in json_dumps({"stderr": SENTINEL, "error": "echo " + SENTINEL})


def test_arbitrary_nonsecret_named_field_can_be_explicitly_migrated(configured):
    client, server, _ = configured
    binding = migrate(client, server, "REGION")
    saved = store.load_agent_settings().mcp.servers[0]
    assert "REGION" not in saved.env
    assert saved.credential_refs["env"]["REGION"] == binding.reference
    # The unselected legacy credential is still marked, never claimed fully migrated.
    payload = client.get("/api/settings").json()["mcp"]["servers"][0]
    assert payload["legacy_secret_fields"]["env"] == ["API_KEY"]


def test_unrelated_whole_settings_save_preserves_hidden_legacy_secret(configured, monkeypatch):
    client, _, path = configured
    monkeypatch.setattr("app.api.routes.settings.restart_enabled_mcp_servers", lambda *_: [])
    monkeypatch.setattr("app.api.routes.settings.reset_runtime_cache", lambda **_: None)
    payload = client.get("/api/settings").json()
    payload["mcp"]["servers"][0]["description"] = "Edited note"
    response = client.put("/api/settings", json={"mcp": payload["mcp"]})
    assert response.status_code == 200
    assert SENTINEL not in response.text
    assert json.loads(path.read_text())["mcp"]["servers"][0]["env"]["API_KEY"] == SENTINEL


@pytest.mark.parametrize("mutation", ["version", "digest", "identity", "transport"])
def test_changed_migration_selection_does_not_remove_plaintext(configured, mutation):
    client, server, path = configured
    body = selection(client, server)
    body["legacy_digest"] = hashlib.sha256(SENTINEL.encode()).hexdigest()
    if mutation == "version": body["expected_version"] = "old"
    if mutation == "digest": body["legacy_digest"] = "0" * 64
    if mutation == "identity": body["binding"]["server_id"] = "0" * 32
    if mutation == "transport": body["binding"].update(transport="streamable_http", target="headers")
    assert client.post(PREFIX + "/commit", json=body, headers=auth()).status_code == 409
    assert SENTINEL in path.read_text()


def test_atomic_migration_write_failure_preserves_original_file(configured, monkeypatch):
    client, server, path = configured
    before = path.read_bytes()
    body = selection(client, server)
    monkeypatch.setattr(store.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("simulated replace failure")))
    with pytest.raises(OSError):
        client.post(PREFIX + "/commit", json={**body, "legacy_digest": hashlib.sha256(SENTINEL.encode()).hexdigest()}, headers=auth())
    assert path.read_bytes() == before
    assert list(path.parent.glob(".settings-*")) == []


def test_missing_hydration_never_launches_then_rehydration_is_server_local(configured, monkeypatch):
    client, server, _ = configured
    first = migrate(client, server)
    second = store.MCPServerConfig(name="two", command=sys.executable)
    other = secrets.CredentialBinding(server_id=second.server_id, transport="stdio", target="env", key="API_KEY")
    second.credential_refs = {"env": {"API_KEY": other.reference}}
    current = store.load_agent_settings()
    current.mcp.servers.append(second)
    store.save_agent_settings(current)
    manager = connection.ServerManager(current.mcp.servers[0])
    monkeypatch.setattr(manager, "_prepare_start_locked", lambda: pytest.fail("must not launch before credentials"))
    assert not manager.start()
    assert manager.status()["state"] == "credentials_unavailable"
    secrets.hydrate({first.reference: SENTINEL, other.reference: "second-private-sentinel"})
    assert secrets.resolved_fields(current.mcp.servers[0])[0]["API_KEY"] == SENTINEL
    assert secrets.resolved_fields(second)[0] == {"API_KEY": "second-private-sentinel"}
    assert "second-private-sentinel" not in json.dumps(secrets.resolved_fields(current.mcp.servers[0]))
    manager.close()


def test_credential_revocation_closes_active_connection_and_blocks_future_launch(configured, monkeypatch):
    client, server, _ = configured
    binding = migrate(client, server)
    secrets.hydrate({binding.reference: SENTINEL})
    closed = []
    monkeypatch.setitem(connection._ACTIVE_MANAGERS, server.name, SimpleNamespace(close=lambda: closed.append(True)))
    response = client.post(PREFIX + "/revoke", json={"binding": binding.model_dump()}, headers=auth())
    assert response.status_code == 200
    assert closed == [True]
    with pytest.raises(ValueError, match="unavailable"):
        secrets.resolved_fields(store.load_agent_settings().mcp.servers[0])
    assert SENTINEL not in json_dumps({"late_error": SENTINEL})


def test_rotation_restarts_affected_connection_with_current_value(configured, monkeypatch):
    client, server, _ = configured
    binding = migrate(client, server)
    secrets.hydrate({binding.reference: SENTINEL})
    current = store.load_agent_settings()
    current.mcp.servers[0].enabled = True
    store.save_agent_settings(current)
    closed, started = [], []
    monkeypatch.setitem(connection._ACTIVE_MANAGERS, server.name, SimpleNamespace(close=lambda: closed.append(True)))
    def ensure(cfg):
        return SimpleNamespace(start=lambda: started.append(secrets.resolved_fields(cfg)[0]["API_KEY"]))
    monkeypatch.setattr(connection, "ensure_mcp_manager", ensure)
    result = client.post(PREFIX + "/hydrate", json={"values": [{"binding": binding.model_dump(), "value": "rotated-private-sentinel"}]}, headers=auth())
    assert result.status_code == 200
    assert closed == [True]
    assert started == ["rotated-private-sentinel"]
    assert SENTINEL not in result.text


@pytest.mark.parametrize("change", ["server_id", "key", "transport"])
def test_reference_cannot_be_rebound(configured, change):
    client, server, _ = configured
    binding = migrate(client, server)
    raw = store.load_agent_settings().mcp.servers[0].model_dump()
    if change == "server_id": raw["server_id"] = "f" * 32
    if change == "key": raw["credential_refs"] = {"env": {"OTHER": binding.reference}}
    if change == "transport": raw["transport"] = "streamable_http"
    with pytest.raises(ValidationError):
        store.MCPServerConfig.model_validate(raw)


def test_new_credentials_and_masked_values_cannot_enter_ordinary_configuration(configured):
    _, server, _ = configured
    current = store.load_agent_settings()
    raw = server.model_dump()
    raw["env"]["NEW_TOKEN"] = "new-value"
    with pytest.raises(ValueError): store.merge_agent_settings(current, {"mcp": {"servers": [raw]}})
    raw["env"] = {"REGION": "[REDACTED]"}
    with pytest.raises(ValueError): store.merge_agent_settings(current, {"mcp": {"servers": [raw]}})


def test_safe_process_environment_does_not_inherit_provider_or_other_mcp_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-private-value")
    monkeypatch.setenv("MONAW_CONTROL_SECRET", "control-private-value")
    monkeypatch.setenv("CUSTOM_SERVICE_PASSWORD", "other-private-value")
    monkeypatch.setenv("PATH", "fixture-path")
    env = secrets.child_environment({"API_KEY": SENTINEL})
    assert env["PATH"] == "fixture-path"
    assert env["API_KEY"] == SENTINEL
    assert not {"OPENAI_API_KEY", "MONAW_CONTROL_SECRET", "CUSTOM_SERVICE_PASSWORD"} & env.keys()
    assert SENTINEL not in os.environ.values()


def test_http_transport_receives_connection_local_headers_without_mutating_config(monkeypatch):
    import mcp.client.streamable_http as http
    cfg = store.MCPServerConfig(name="remote", transport="streamable_http", url="http://127.0.0.1:1/mcp", headers={"Accept": "application/json"})
    binding = secrets.CredentialBinding(server_id=cfg.server_id, transport=cfg.transport, target="headers", key="Authorization")
    cfg.credential_refs = {"headers": {"Authorization": binding.reference}}
    secrets.hydrate({binding.reference: SENTINEL})
    captured = []
    @asynccontextmanager
    async def transport(url, **kwargs):
        captured.append(kwargs["headers"])
        yield (None, None, None)
    monkeypatch.setattr(http, "streamablehttp_client", transport)
    manager = connection.ServerManager(cfg)
    async def initialized(*args): pass
    monkeypatch.setattr(manager, "_initialize_session", initialized)
    asyncio.run(manager._serve())
    assert captured == [{"Accept": "application/json", "Authorization": SENTINEL}]
    assert cfg.headers == {"Accept": "application/json"}
    assert SENTINEL not in cfg.model_dump_json()


def test_credential_stderr_is_bounded_redacted_memory_without_plaintext_file():
    secrets.remember_secrets([SENTINEL])
    capture = connection._CredentialStderr(limit=256)
    try:
        subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write('x' * 1024 + sys.argv[1])", SENTINEL], stderr=capture, check=True)
        capture.close()
        assert SENTINEL not in capture.getvalue()
        assert "[REDACTED]" in capture.getvalue()
        assert len(capture.getvalue()) <= 256
        assert not hasattr(capture, "_temp_dir")
    finally:
        capture.close()


def test_removed_server_revokes_resolution_and_retained_callbacks_cannot_restart(configured, monkeypatch):
    client, server, _ = configured
    binding = migrate(client, server)
    secrets.hydrate({binding.reference: SENTINEL})
    cfg = store.load_agent_settings().mcp.servers[0]
    manager = connection.ensure_mcp_manager(cfg)
    monkeypatch.setattr("app.api.routes.settings.restart_enabled_mcp_servers", lambda *_: [])
    response = client.put("/api/settings", json={"mcp": {"servers": []}})
    assert response.status_code == 200
    assert not manager.start()
    assert json.loads(manager.call_tool_sync("get_status", {}))["error"]["code"] == "mcp_credentials_changed"
    with pytest.raises(ValueError, match="unavailable"):
        secrets.resolved_fields(cfg)
