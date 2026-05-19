import json
import os
import time
from types import SimpleNamespace

from app.skills.exec import tools as exec_tools


def test_exec_default_workdir_uses_agent_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(exec_tools, "WORKSPACE_DIR", tmp_path)

    assert exec_tools._effective_workdir("") == str(tmp_path)


def test_exec_tool_returns_pending_approval(monkeypatch):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=True,
            reason="approval required",
            reason_code="confirmation_required",
            policy_source="settings.json",
        ),
    )
    monkeypatch.setattr(
        exec_tools,
        "create_ticket",
        lambda **_kwargs: SimpleNamespace(id="ticket-123"),
    )

    result = json.loads(exec_tools.exec_tool("Write-Host hi"))

    assert result["status"] == "pending_approval"
    assert result["ticket_id"] == "ticket-123"


def test_exec_approval_ticket_redacts_env_values(monkeypatch):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=True,
            reason="approval required",
            reason_code="confirmation_required",
            policy_source="settings.json",
        ),
    )
    captured = {}

    def fake_create_ticket(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="ticket-123")

    monkeypatch.setattr(exec_tools, "create_ticket", fake_create_ticket)

    result = json.loads(
        exec_tools.exec_tool(
            "Write-Host hi",
            env={"MONAW_TEST_VALUE": "super-secret"},
        )
    )
    ticket_args = captured["payload"]["args"]
    ticket_input = json.loads(captured["payload"]["input_str"])

    assert result["env_keys"] == ["MONAW_TEST_VALUE"]
    assert ticket_args["env"] == {"MONAW_TEST_VALUE": "<redacted>"}
    assert ticket_input["env"] == {"MONAW_TEST_VALUE": "<redacted>"}
    assert ticket_args["sandbox"]["env_inheritance"] == "scrubbed"
    assert "super-secret" not in json.dumps(captured["payload"])


def test_exec_tool_runs_command(monkeypatch, tmp_path):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=False,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    )

    result = json.loads(
        exec_tools.exec_tool(
            "Write-Host hello",
            shell="powershell",
            workdir=str(tmp_path),
        )
    )

    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello\n"
    assert result["duration_ms"] >= 0


def test_exec_resume_bypasses_confirmation_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=True,
            reason="approval required",
            reason_code="confirmation_required",
            policy_source="settings.json",
        ),
    )

    result = json.loads(
        exec_tools.exec_tool(
            "Write-Host approved",
            workdir=str(tmp_path),
            _bypass_gate=True,
        )
    )

    assert result["status"] == "ok"
    assert result["stdout"] == "approved\n"


def test_exec_session_start_poll_and_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=False,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    )

    shell = "cmd" if os.name == "nt" else "bash"
    started = json.loads(
        exec_tools.exec_start(
            "echo hello",
            shell=shell,
            workdir=str(tmp_path),
        )
    )

    assert started["status"] == "running"
    assert started["command_id"]

    command_id = started["command_id"]
    for _ in range(20):
        polled = json.loads(exec_tools.exec_poll(command_id, return_mode="full"))
        if polled.get("exit_code") is not None:
            break
        time.sleep(0.05)

    assert polled["status"] == "ok"
    assert polled["exit_code"] == 0
    assert "hello" in polled["stdout"]
    assert polled["stdout_path"]
