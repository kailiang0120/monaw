"""The support report: readable before sharing, bounded, redacted, and without setting values."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.agent.observability import support_report
from app.agent.observability.support_report import MAX_REPORT_BYTES, build_support_report, settings_shape
from app.agent.settings_store import AgentSettings, MCPServerConfig
from app.main import app

SECRET = "sk-supportreportsentinel0123456789"
PRIVATE_PATH = r"C:\Users\Kai\Documents\payroll.xlsx"


def _report(**overrides):
    values = {
        "created_at": "2026-10-31T00:00:00Z",
        "app_version": "1.0.0",
        "summary": {"backend": {"version": "1.0.0"}},
        "configuration": {},
        "errors": [],
        "log_lines": None,
    }
    values.update(overrides)
    return build_support_report(**values)


def test_configuration_shape_has_counts_and_modes_but_no_values():
    settings = AgentSettings()
    settings.mcp.servers = [
        MCPServerConfig(name="payroll-db", command=r"C:\tools\payroll.exe", env={"API_KEY": SECRET}, enabled=True),
        MCPServerConfig(name="notes", command="npx", enabled=False),
    ]

    shape = settings_shape(settings)
    text = json.dumps(shape)

    assert shape["mcp"] == {"enabled": True, "servers": 2, "enabled_servers": 1}
    assert shape["permission_mode"] == settings.permissions.mode
    for value in (SECRET, "payroll", "API_KEY", "npx"):
        assert value not in text


def test_errors_and_log_lines_are_redacted():
    report = _report(
        errors=[{"message": f"call failed with key {SECRET} reading {PRIVATE_PATH}"}],
        log_lines=[f"Authorization: Bearer {SECRET}", f"opened {PRIVATE_PATH}"],
    )
    text = json.dumps(report)

    assert SECRET not in text
    assert PRIVATE_PATH not in text
    assert "[REDACTED PATH]" in text
    assert report["backend_log"]["included"] is True


def test_log_is_left_out_without_support_mode():
    report = _report(log_lines=None)

    assert report["backend_log"] == {
        "included": False,
        "lines": [],
        "note": "Turn on support mode to include the backend log.",
    }


def test_report_stays_under_the_size_limit_keeping_the_newest_entries():
    lines = [f"line {index} " + "x" * 4000 for index in range(300)]
    errors = [{"message": f"error {index} " + "y" * 4000} for index in range(50)]

    report = _report(log_lines=lines, errors=errors)

    assert len(json.dumps(report, ensure_ascii=False).encode("utf-8")) <= MAX_REPORT_BYTES
    assert report["truncated"] is True
    assert report["backend_log"]["lines"][-1].startswith("line 299 ")
    assert report["recent_errors"][0]["message"].startswith("error 0 ")


def test_route_includes_the_redacted_log_only_in_support_mode(monkeypatch, tmp_path):
    from app.agent.observability import recorder as recorder_module
    from app.agent.observability.recorder import ObservabilityRecorder

    monkeypatch.setattr(recorder_module, "_RECORDER", ObservabilityRecorder(root=tmp_path / "obs"))
    log_path = tmp_path / "backend.log"
    log_path.write_text(f"startup with key {SECRET}\n", encoding="utf-8")
    monkeypatch.setattr(recorder_module, "BACKEND_LOG_PATH", log_path)
    client = TestClient(app)

    without = client.get("/api/diagnostics/support-report")
    client.post("/api/observability/support-mode?duration_seconds=60")
    try:
        with_log = client.get("/api/diagnostics/support-report")
    finally:
        client.delete("/api/observability/support-mode")

    assert without.status_code == 200
    assert without.json()["format"] == support_report.REPORT_FORMAT
    assert without.json()["backend_log"]["included"] is False
    assert with_log.json()["backend_log"]["included"] is True
    assert any("startup with key" in line for line in with_log.json()["backend_log"]["lines"])
    assert SECRET not in with_log.text
