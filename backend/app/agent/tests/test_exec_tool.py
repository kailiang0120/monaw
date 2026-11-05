import json
import os
import time
from types import SimpleNamespace

from app.agent.sandbox.models import SandboxDecision
from app.skills.exec import tools as exec_tools


def _central_default_permissions(monkeypatch):
    from app.agent.settings_store import AgentSettings
    from app.agent import controller_policy
    settings = AgentSettings()
    settings.permissions.mode = "default"
    monkeypatch.setattr(controller_policy, "_load_runtime_settings", lambda: settings)


def test_exec_default_workdir_uses_agent_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(exec_tools, "WORKSPACE_DIR", tmp_path)

    assert exec_tools._effective_workdir("") == str(tmp_path)


def test_exec_tool_returns_pending_approval(monkeypatch):
    _central_default_permissions(monkeypatch)
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
    from app.agent.approval_broker import get_ticket
    assert get_ticket(result["ticket_id"]).authorization_binding


def test_host_tool_approval_preserves_sandbox_reason(monkeypatch, tmp_path):
    _central_default_permissions(monkeypatch)
    import app.agent.approval_broker as broker
    real_create = broker.create_ticket
    captured = {}
    monkeypatch.setattr(
        exec_tools,
        "build_sandbox_decision",
        lambda **_kwargs: SandboxDecision(
            allowed=True,
            required=False,
            profile="standard",
            backend="local_restricted",
            mode="auto",
            security_label="advisory",
            network="deny",
            network_enforcement="advisory",
            write_strategy="copy_out",
            requested_shell="auto",
            effective_shell="powershell",
            filesystem_policy="host",
            explicit_approval_required=True,
            reason="The pinned Docker image does not provide 'git'; this command will use the advisory host runner after explicit approval",
            reason_code="docker_host_tool_fallback_requires_approval",
        ),
    )
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=True,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    )

    def fake_create_ticket(**kwargs):
        captured.update(kwargs)
        return real_create(**kwargs)

    monkeypatch.setattr(broker, "create_ticket", fake_create_ticket)

    result = json.loads(
        exec_tools.exec_tool("git status", workdir=str(tmp_path))
    )

    assert result["status"] == "pending_approval"
    assert result["sandbox"]["reason_code"] == "docker_host_tool_fallback_requires_approval"
    assert "'git'" in result["sandbox"]["reason"]
    assert captured["reason"] == result["reason"]


def test_exec_approval_ticket_redacts_env_values(monkeypatch):
    _central_default_permissions(monkeypatch)
    import app.agent.approval_broker as broker
    real_create = broker.create_ticket
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
        return real_create(**kwargs)

    monkeypatch.setattr(broker, "create_ticket", fake_create_ticket)

    result = json.loads(
        exec_tools.exec_tool(
            "Write-Host hi",
            env={"MONAW_TEST_VALUE": "super-secret"},
        )
    )
    ticket_args = captured["payload"]["args"]

    assert result["env_keys"] == ["MONAW_TEST_VALUE"]
    assert ticket_args["env"] == {"MONAW_TEST_VALUE": "<redacted>"}
    assert ticket_args["sandbox"]["env_inheritance"] == "scrubbed"
    assert "super-secret" not in json.dumps(captured["payload"])


def test_exec_tool_runs_command(monkeypatch, tmp_path):
    from app.agent.settings_store import AgentSettings
    from app.agent.sandbox.models import SandboxCapabilities, SandboxBackendCapability
    settings = AgentSettings()
    settings.sandbox.mode = "host"
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", settings)
    monkeypatch.setattr("app.agent.sandbox.manager.probe_capabilities", lambda _settings: SandboxCapabilities(
        docker=SandboxBackendCapability(backend="docker", enabled=False, available=False, security_label="strong", network_enforcement="enforced"),
        local_restricted=SandboxBackendCapability(backend="local_restricted", enabled=False, available=False, security_label="advisory", network_enforcement="advisory")))
    monkeypatch.setattr(
        exec_tools,
        "build_sandbox_decision",
        lambda **_kwargs: SandboxDecision(
            allowed=True,
            required=False,
            profile="standard",
            backend="local_direct",
            mode="host",
            security_label="none",
            network="deny",
            network_enforcement="none",
            write_strategy="direct_rw",
            filesystem_policy="host",
            reason_code="allowed",
        ),
    )
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
        "build_sandbox_decision",
        lambda **_kwargs: SandboxDecision(
            allowed=True,
            required=False,
            profile="standard",
            backend="local_direct",
            mode="host",
            security_label="none",
            network="deny",
            network_enforcement="none",
            write_strategy="direct_rw",
            reason_code="allowed",
        ),
    )
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
    # Windows PowerShell process startup can exceed one second on loaded CI.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        polled = json.loads(exec_tools.exec_poll(command_id, return_mode="full"))
        if polled.get("exit_code") is not None:
            break
        time.sleep(0.05)

    assert polled["status"] == "ok"
    assert polled["exit_code"] == 0
    assert "hello" in polled["stdout"]
    assert polled["stdout_path"]
