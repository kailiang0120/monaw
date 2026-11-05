import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent.settings_store import AgentSettings, load_agent_settings, merge_agent_settings, save_agent_settings
from app.main import app
from app.schemas import SettingsUpdate


def test_execution_detail_defaults_and_survives_restart_reload(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"identity": {"agent_name": "Monaw"}}), encoding="utf-8")

    loaded = load_agent_settings(settings_path=settings_path)
    assert loaded.execution_detail == "simple"

    updated = merge_agent_settings(loaded, {"execution_detail": "developer"})
    save_agent_settings(updated, settings_path=settings_path)

    restarted = load_agent_settings(settings_path=settings_path)
    assert restarted.execution_detail == "developer"


def test_execution_detail_accepts_only_display_modes():
    assert SettingsUpdate.model_validate({"execution_detail": "detailed"}).execution_detail == "detailed"
    with pytest.raises(ValidationError):
        SettingsUpdate.model_validate({"execution_detail": "unrestricted"})


def test_execution_detail_update_persists_without_resetting_agent_runtime(monkeypatch):
    current = AgentSettings()
    saved = []
    resets = []
    published = []
    monkeypatch.setattr("app.api.routes.settings.load_agent_settings", lambda _settings: current)
    monkeypatch.setattr("app.api.routes.settings.save_agent_settings", lambda updated: saved.append(updated) or updated)
    monkeypatch.setattr("app.api.routes.settings.reset_runtime_cache", lambda **kwargs: resets.append(kwargs))
    monkeypatch.setattr("app.api.routes.settings.publish_ui_event", lambda name, data: published.append((name, data)))

    response = TestClient(app).put("/api/settings", json={"execution_detail": "detailed"})

    assert response.status_code == 200
    assert response.json()["execution_detail"] == "detailed"
    assert saved[0].execution_detail == "detailed"
    assert resets == []
    assert any(name == "settings.changed" for name, _data in published)
