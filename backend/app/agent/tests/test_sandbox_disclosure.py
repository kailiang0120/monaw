import json
from types import SimpleNamespace

import pytest

from app.agent.approval_broker import get_ticket
from app.agent.execution_gate import ExecutionGateService
from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.sandbox.manager import SandboxManager
from app.agent.sandbox.models import SandboxBackendCapability, SandboxCapabilities, SandboxExecutionResult
from app.agent.settings_store import AgentSettings
from app.skills.exec import tools


@pytest.fixture
def environment(monkeypatch, tmp_path):
    settings = AgentSettings()
    settings.sandbox.allowed_bind_roots = [str(tmp_path)]
    capabilities = SandboxCapabilities(
        docker=SandboxBackendCapability(backend="docker", enabled=True, available=True,
            security_label="strong", network_enforcement="enforced"),
        local_restricted=SandboxBackendCapability(backend="local_restricted", enabled=False,
            available=False, security_label="advisory", network_enforcement="advisory"),
    )
    monkeypatch.setattr(tools, "_ACTIVE_SETTINGS", settings)
    monkeypatch.setattr(tools, "load_agent_settings", lambda: settings)
    monkeypatch.setattr("app.agent.sandbox.policy.probe_capabilities", lambda _: capabilities)
    monkeypatch.setattr("app.agent.sandbox.manager.probe_capabilities", lambda _: capabilities)
    monkeypatch.setattr(tools, "resolve_permission", lambda *a, **kw: SimpleNamespace(
        blocked=False, requires_access_grant=False, requires_confirmation=True,
        reason="Confirm command", reason_code="confirmation_required", policy_source="settings"))
    dispatched = []

    def run(_runner, request):
        dispatched.append(request)
        return SandboxExecutionResult(status="ok", sandbox=request.env_metadata)

    for backend in ("DockerRunner", "LocalDirectRunner", "LocalRestrictedRunner"):
        monkeypatch.setattr(f"app.agent.sandbox.manager.{backend}.run", run)
        monkeypatch.setattr(f"app.agent.sandbox.manager.{backend}.is_available", lambda _: True)
    return settings, capabilities, dispatched, tmp_path


@pytest.mark.parametrize("change", ["backend", "workspace", "network", "writes", "image", "root"])
def test_approved_exec_reprompts_when_its_disclosed_environment_changes(environment, monkeypatch, change):
    settings, capabilities, dispatched, workdir = environment
    pending = json.loads(tools.exec_tool("echo hi", shell="bash", workdir=str(workdir)))
    ticket = get_ticket(pending["ticket_id"])
    assert ticket.payload["args"]["sandbox"]["execution_environment"]["backend"] == "docker"
    if change == "backend":
        capabilities.docker.available = False
    elif change == "workspace":
        monkeypatch.setattr(tools, "_effective_workdir", lambda _: str(workdir / "other"))
    elif change == "network":
        settings.sandbox.network.default = "allow"
    elif change == "writes":
        settings.sandbox.default_write_strategy = "direct_rw"
    elif change == "image":
        settings.sandbox.docker.image = "python@sha256:" + "b" * 64
    else:
        settings.sandbox.docker.read_only_root = False
    result = json.loads(tools._resume_exec(ticket.payload["input_str"]))
    assert result["status"] == "pending_approval"
    assert result["ticket_id"] != ticket.id
    assert not dispatched
    current = result["sandbox"]["execution_environment"]
    assert current != pending["sandbox"]["execution_environment"] or change == "root"


def test_unchanged_approval_discloses_same_environment_in_event_and_result(environment):
    _, _, dispatched, workdir = environment
    pending = json.loads(tools.exec_tool("echo hi", shell="bash", workdir=str(workdir)))
    ticket = get_ticket(pending["ticket_id"])
    result = json.loads(tools._resume_exec(ticket.payload["input_str"]))
    assert result["status"] == "ok"
    assert len(dispatched) == 1
    expected = pending["sandbox"]["execution_environment"]
    assert ExecutionGateService.check_pending_status(pending)["data"]["execution_environment"] == expected
    assert result["sandbox"]["execution_environment"] == expected


@pytest.mark.parametrize("strategy,description", [
    ("discard", "host workspace is not exposed"),
    ("copy_out", "temporary copy"),
    ("direct_rw", "mounted read/write"),
])
def test_docker_disclosure_agrees_with_saved_diagnostics(environment, strategy, description):
    settings, capabilities, _, workdir = environment
    settings.sandbox.default_write_strategy = strategy
    pending = json.loads(tools.exec_tool("echo hi", shell="bash", workdir=str(workdir)))
    disclosed = pending["sandbox"]["execution_environment"]
    assert description in disclosed["filesystem"]
    assert disclosed["network"] == "Network denied by Docker"
    diagnostic = get_sandbox_status(settings.sandbox, capabilities=capabilities,
        permissions=settings.permissions)["execution_environment"]
    assert diagnostic == disclosed


@pytest.mark.parametrize("mode", ["host", "auto", "local_restricted"])
def test_host_disclosure_does_not_claim_configured_deny_or_discard(environment, mode):
    settings, capabilities, _, workdir = environment
    settings.sandbox.mode = mode
    capabilities.docker.available = False
    capabilities.local_restricted.available = capabilities.local_restricted.enabled = True
    pending = json.loads(tools.exec_tool("echo hi", shell="bash", workdir=str(workdir)))
    disclosed = pending["sandbox"]["execution_environment"]
    assert "no filesystem or network isolation" in disclosed["summary"]
    assert "not enforced" in disclosed["network"]
    assert "host files directly" in disclosed["writes"]
    assert "personal Chrome" in disclosed["scope"]


def test_final_dispatch_refuses_changed_container_policy(environment, monkeypatch):
    settings, capabilities, dispatched, workdir = environment
    monkeypatch.setattr(tools, "resolve_permission", lambda *a, **kw: SimpleNamespace(
        blocked=False, requires_access_grant=False, requires_confirmation=False))
    original = SandboxManager.run

    def change_before_dispatch(manager, request, *, decision=None):
        manager.settings.docker.read_only_root = False
        return original(manager, request, decision=decision)

    monkeypatch.setattr(SandboxManager, "run", change_before_dispatch)
    result = json.loads(tools.exec_tool("echo hi", shell="bash", workdir=str(workdir)))
    assert result["status"] == "blocked"
    assert result["reason_code"] == "execution_environment_changed"
    assert result["sandbox"]["execution_environment"]["summary"] == "Blocked before execution"
    assert not dispatched


def test_long_running_host_session_keeps_its_disclosure_when_polled(environment):
    settings, _, _, workdir = environment
    settings.sandbox.mode = "host"
    pending = json.loads(tools.exec_start("Write-Output hi", shell="powershell", workdir=str(workdir)))
    ticket = get_ticket(pending["ticket_id"])
    started = json.loads(tools._resume_exec_start(ticket.payload["input_str"]))
    assert started["status"] == "running"
    try:
        polled = json.loads(tools.exec_poll(started["command_id"]))
        assert "no filesystem or network isolation" in started["sandbox"]["execution_environment"]["summary"]
        assert polled["sandbox"]["execution_environment"] == pending["sandbox"]["execution_environment"]
    finally:
        tools.exec_stop(started["command_id"], "kill")
