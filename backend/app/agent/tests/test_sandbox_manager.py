import json
import os
import shlex
import sys
import threading
import time
import pytest
from types import SimpleNamespace

from app.agent.sandbox.backends.local_direct import LocalDirectRunner
from app.agent.sandbox.backends.local_restricted import LocalRestrictedRunner
from app.agent.sandbox.environment import build_exec_environment
from app.agent.sandbox.manager import SandboxManager
from app.agent.sandbox.models import (
    SandboxRunRequest,
    SandboxBackendCapability,
    SandboxCapabilities,
    SandboxExecutionRequest,
    SandboxExecutionResult,
)
from app.agent.settings_store import AgentSettings
from app.agent.sandbox.policy import SandboxPolicy
from app.skills.exec import tools as exec_tools


def _allow_exec(monkeypatch, *, confirmation=False):
    monkeypatch.setattr(
        exec_tools,
        "resolve_permission",
        lambda *_args, **_kwargs: SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=confirmation,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    )


def test_caller_profile_cannot_hide_a_blocked_command():
    settings = AgentSettings().sandbox
    decision = SandboxPolicy(settings, capabilities=_capabilities()).decide(
        SandboxRunRequest(command=r"reg delete HKCU\Software\Thing", profile="standard"))
    assert not decision.allowed
    assert decision.reason_code == "command_blocked"


def test_manager_revalidates_command_instead_of_trusting_preview():
    settings = AgentSettings().sandbox
    policy = SandboxPolicy(settings, capabilities=_capabilities())
    preview = policy.decide(SandboxRunRequest(command="echo safe"))
    request = _request(r"reg delete HKCU\Software\Thing")
    result = SandboxManager(settings, capabilities=_capabilities()).run(request, decision=preview)
    assert result.status == "blocked"
    assert result.reason_code == "command_blocked"


def test_network_request_cannot_override_configured_deny():
    decision = SandboxPolicy(AgentSettings().sandbox, capabilities=_capabilities(docker=True)).decide(
        SandboxRunRequest(command="echo safe", network="allow"))
    assert not decision.allowed
    assert decision.reason_code == "network_access_disabled"


def test_manager_bounds_resources_from_actual_settings(monkeypatch):
    settings = AgentSettings().sandbox
    settings.mode = "host"
    settings.resources.timeout_seconds = 10
    request = _request()
    request.timeout = 500
    request.resources = {"memory_mb": 999999, "timeout_seconds": 500}
    manager = SandboxManager(settings, capabilities=_capabilities(local=False))
    captured = []
    monkeypatch.setattr(manager.local_direct, "run", lambda req: captured.append(req) or SandboxExecutionResult(status="ok"))
    assert manager.run(request).status == "ok"
    assert captured[0].timeout == 10
    assert captured[0].resources == settings.resources.model_dump()


@pytest.mark.parametrize("mode,confirmation", [("default", True), ("full_access", False), ("auto_review", True)])
def test_host_routing_follows_permission_mode(mode, confirmation):
    from app.agent.settings_store import merge_agent_settings
    settings = merge_agent_settings(AgentSettings(), {"permissions": {"mode": mode}})
    settings.sandbox.mode = "host"
    decision = SandboxPolicy(settings.sandbox, capabilities=_capabilities(), permissions=settings.permissions).decide(
        SandboxRunRequest(command="echo safe"))
    assert decision.allowed
    assert decision.explicit_approval_required is confirmation


def test_strict_sandbox_stays_blocked_in_full_access_without_docker():
    settings = AgentSettings()
    settings.permissions.mode = "full_access"
    settings.sandbox.mode = "enforce"
    decision = SandboxPolicy(settings.sandbox, capabilities=_capabilities(), permissions=settings.permissions).decide(
        SandboxRunRequest(command="echo safe"))
    assert not decision.allowed
    assert decision.backend == "none"


def _settings_with_sandbox(**patch):
    settings = AgentSettings()
    for key, value in patch.items():
        setattr(settings.sandbox, key, value)
    return SimpleNamespace(sandbox=settings.sandbox)


def _capability(
    backend: str,
    *,
    available: bool,
    security_label: str,
    network_enforcement: str,
    reason: str = "",
) -> SandboxBackendCapability:
    return SandboxBackendCapability(
        backend=backend,  # type: ignore[arg-type]
        enabled=True,
        available=available,
        security_label=security_label,  # type: ignore[arg-type]
        network_enforcement=network_enforcement,  # type: ignore[arg-type]
        reason=reason,
    )


def _capabilities(*, docker: bool = False, local: bool = True) -> SandboxCapabilities:
    return SandboxCapabilities(
        docker=_capability(
            "docker",
            available=docker,
            security_label="strong",
            network_enforcement="enforced",
            reason="" if docker else "docker_not_found",
        ),
        local_restricted=_capability(
            "local_restricted",
            available=local,
            security_label="advisory",
            network_enforcement="advisory",
            reason="" if local else "disabled",
        ),
    )


def _python_command(code: str) -> tuple[str, str]:
    if os.name == "nt":
        return "powershell", f'& "{sys.executable}" -c "{code}"'
    return "bash", f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _stdout_stderr_exit_command() -> tuple[str, str]:
    if os.name == "nt":
        return "powershell", "Write-Output 'out'; [Console]::Error.WriteLine('err'); exit 3"
    return _python_command("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)")


def _request(command: str = "Write-Output ok", *, env: dict[str, str] | None = None) -> SandboxExecutionRequest:
    shell = "powershell" if os.name == "nt" else "bash"
    sanitized_env = build_exec_environment(
        requested_env=env or {},
        shell=shell,
        profile="standard",
        backend="local_direct",
    )
    return SandboxExecutionRequest(
        command=command,
        shell=shell,
        env=sanitized_env.env,
        profile="standard",
        env_metadata={
            "mode": "auto",
            **sanitized_env.metadata(),
        },
    )


def test_exec_routes_through_sandbox_manager(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    captured = {}

    class FakeManager:
        def __init__(self, settings):
            captured["settings"] = settings

        def run(self, request, *, decision=None):
            captured["request"] = request
            return SandboxExecutionResult(
                status="ok",
                exit_code=0,
                stdout="managed\n",
                shell=request.shell,
                workdir=request.workdir,
                sandbox={"backend": "local_direct", "security_label": "none"},
            )

    monkeypatch.setattr(exec_tools, "SandboxManager", FakeManager)
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", _settings_with_sandbox())

    result = json.loads(
        exec_tools.exec_tool("Write-Output ignored", workdir=str(tmp_path), _bypass_gate=True)
    )

    assert result["stdout"] == "managed\n"
    assert captured["request"].command == "Write-Output ignored"
    assert captured["request"].workdir == str(tmp_path)
    assert captured["request"].backend in {"local_direct", "local_restricted"}


def test_exec_tool_blocks_blocked_policy_before_runner(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)

    class ExplodingManager:
        def __init__(self, _settings):
            pass

        def run(self, _request):
            raise AssertionError("blocked command reached runner")

    monkeypatch.setattr(exec_tools, "SandboxManager", ExplodingManager)
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", _settings_with_sandbox())

    result = json.loads(
        exec_tools.exec_tool(
            r"reg delete HKCU\Software\Thing",
            shell="powershell",
            workdir=str(tmp_path),
            _bypass_gate=True,
        )
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "command_blocked"
    assert result["sandbox"]["profile"] == "blocked"


def test_exec_tool_offers_approval_for_untrusted_when_strong_backend_unavailable(monkeypatch, tmp_path):
    _allow_exec(monkeypatch, confirmation=True)
    settings = AgentSettings()
    settings.sandbox.docker.enabled = False
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", SimpleNamespace(sandbox=settings.sandbox))

    result = json.loads(
        exec_tools.exec_tool("npm install", shell="bash", workdir=str(tmp_path))
    )

    assert result["status"] == "pending_approval"
    assert result["sandbox"]["profile"] == "untrusted"


def test_approval_preview_matches_execution_decision(monkeypatch, tmp_path):
    captured_ticket = {}
    captured_run = {}
    permission_decisions = [
        SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=True,
            reason="approval required",
            reason_code="confirmation_required",
            policy_source="settings.json",
        ),
        SimpleNamespace(
            blocked=False,
            requires_access_grant=False,
            requires_confirmation=False,
            reason="",
            reason_code="allowed",
            policy_source="settings.json",
        ),
    ]

    def fake_permission(*_args, **_kwargs):
        return permission_decisions.pop(0)

    class FakeManager:
        def __init__(self, _settings):
            pass

        def run(self, request, *, decision=None):
            captured_run["sandbox"] = dict(request.env_metadata)
            return SandboxExecutionResult(
                status="ok",
                exit_code=0,
                stdout="ok\n",
                shell=request.shell,
                workdir=request.workdir,
                sandbox=dict(request.env_metadata),
            )

    def fake_create_ticket(**kwargs):
        captured_ticket["kwargs"] = kwargs
        return SimpleNamespace(id="ticket-123")

    monkeypatch.setattr(exec_tools, "resolve_permission", fake_permission)
    monkeypatch.setattr(exec_tools, "create_ticket", fake_create_ticket)
    monkeypatch.setattr(exec_tools, "SandboxManager", FakeManager)
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", _settings_with_sandbox())

    pending = json.loads(exec_tools.exec_tool("Write-Host hi", workdir=str(tmp_path)))
    completed = json.loads(exec_tools.exec_tool("Write-Host hi", workdir=str(tmp_path), _bypass_gate=True))

    preview = captured_ticket["kwargs"]["payload"]["args"]["sandbox"]
    actual = captured_run["sandbox"]

    assert pending["status"] == "pending_approval"
    assert completed["status"] == "ok"
    for key in ("mode", "profile", "selected_backend", "security_label", "network", "network_enforcement"):
        assert preview[key] == actual[key]


def test_auto_mode_falls_back_to_direct_host_when_no_advisory_backend_is_available():
    result = SandboxManager(
        AgentSettings().sandbox,
        capabilities=_capabilities(local=False),
    ).run(_request())

    assert result.status == "ok"
    assert result.sandbox["backend"] == "local_direct"


def test_off_mode_disables_execution():
    settings = AgentSettings()
    settings.sandbox.mode = "off"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "shell_execution_disabled"


def test_enforce_mode_blocks_without_real_backend():
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "none"


def test_requested_docker_backend_unavailable_before_implementation():
    settings = AgentSettings()
    settings.sandbox.mode = "docker"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "none"


def test_auto_manager_uses_the_policy_selected_advisory_runner():
    result = SandboxManager(
        AgentSettings().sandbox,
        capabilities=_capabilities(local=True),
    ).run(_request())

    assert result.status == "ok"
    assert result.sandbox["backend"] == "local_restricted"


def test_manager_rejects_request_backend_that_disagrees_with_policy():
    request = _request()
    request.backend = "docker"
    result = SandboxManager(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=False, local=True),
    ).run(request)

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_mismatch"
    assert result.sandbox["selected_backend"] == "local_restricted"


def test_strict_docker_modes_block_host_tools_instead_of_falling_back():
    for mode in ("docker", "enforce"):
        settings = AgentSettings()
        settings.sandbox.mode = mode
        request = _request("git status")
        request.shell = "bash"

        result = SandboxManager(
            settings.sandbox,
            capabilities=_capabilities(docker=True, local=True),
        ).run(request)

        assert result.status == "blocked"
        assert result.reason_code == "docker_command_not_compatible"
        assert result.sandbox["selected_backend"] == "none"


def test_manager_does_not_call_docker_for_auto_host_tool_fallback(monkeypatch, tmp_path):
    settings = AgentSettings()
    capabilities = _capabilities(docker=True, local=True)
    from app.agent.sandbox.models import SandboxRunRequest
    from app.agent.sandbox.policy import SandboxPolicy

    decision = SandboxPolicy(settings.sandbox, capabilities=capabilities).decide(
        SandboxRunRequest(command="git status")
    )
    manager = SandboxManager(settings.sandbox, capabilities=capabilities)
    monkeypatch.setattr(
        manager.docker,
        "run",
        lambda _request: (_ for _ in ()).throw(AssertionError("Docker should not run")),
    )
    monkeypatch.setattr(
        manager.local_restricted,
        "run",
        lambda request: SandboxExecutionResult(
            status="ok",
            exit_code=0,
            shell=request.shell,
            workdir=request.workdir,
            sandbox={"backend": "local_restricted"},
        ),
    )
    request = _request("git status")
    request.shell = decision.effective_shell  # type: ignore[assignment]
    request.backend = decision.backend

    result = manager.run(request, decision=decision)

    assert result.status == "ok"


def test_enforce_strong_does_not_accept_advisory_backend():
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities(local=True)).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "none"


def test_exec_tool_enforce_mode_blocks_without_real_backend(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"
    settings.sandbox.docker.enabled = False
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", SimpleNamespace(sandbox=settings.sandbox))

    result = json.loads(
        exec_tools.exec_tool("Write-Output should-not-run", workdir=str(tmp_path), _bypass_gate=True)
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "sandbox_backend_unavailable"
    assert result["sandbox"]["selected_backend"] == "none"


def test_local_direct_runner_preserves_stdout_stderr_exit_code(tmp_path):
    shell, command = _stdout_stderr_exit_command()
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)

    result = LocalDirectRunner().run(request)

    assert result.status == "error"
    assert result.exit_code == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_local_direct_runner_uses_sanitized_env_only(tmp_path):
    shell, command = _python_command("import os; print(os.getenv('MONAW_TEST_VALUE') or '')")
    request = _request(command, env={"MONAW_TEST_VALUE": "hello"})
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)

    result = LocalDirectRunner().run(request)

    assert result.status == "ok"
    assert result.stdout.strip() == "hello"
    assert result.env_keys == ["MONAW_TEST_VALUE"]


def test_sandbox_metadata_is_honest_for_local_direct(tmp_path):
    shell, command = _python_command("print('ok')")
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)

    result = LocalDirectRunner().run(request)

    assert result.sandbox["backend"] == "local_direct"
    assert result.sandbox["security_label"] == "none"
    assert result.sandbox["filesystem_isolation"] == "none"
    assert result.sandbox["process_isolation"] == "none"
    assert result.sandbox["network_isolation"] == "none"


def test_local_restricted_timeout_kills_process(tmp_path):
    if os.name == "nt":
        shell, command = "powershell", "Start-Sleep -Seconds 5"
    else:
        shell, command = "bash", "sleep 5"
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)
    request.timeout = 1

    result = LocalRestrictedRunner().run(request)

    assert result.status == "error"
    assert result.timed_out is True
    assert result.sandbox["backend"] == "local_restricted"
    assert result.sandbox["process_cleanup"] in {"taskkill_process_tree", "process_group"}


def test_local_direct_clamps_timeout_to_resource_limit(tmp_path):
    if os.name == "nt":
        shell, command = "powershell", "Start-Sleep -Seconds 5"
    else:
        shell, command = "bash", "sleep 5"
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)
    request.timeout = 5
    request.resources = {"timeout_seconds": 1}

    result = LocalDirectRunner().run(request)

    assert result.status == "error"
    assert result.timed_out is True


def test_local_direct_cancellation_kills_the_running_process_tree():
    request = _request("Start-Sleep -Seconds 10" if os.name == "nt" else "sleep 10")
    cancel_event = threading.Event()
    request.cancel_event = cancel_event

    def cancel_shortly_after_start() -> None:
        time.sleep(0.2)
        cancel_event.set()

    trigger = threading.Thread(target=cancel_shortly_after_start, daemon=True)
    trigger.start()
    started = time.monotonic()
    result = LocalDirectRunner().run(request)
    trigger.join(timeout=1)

    assert result.cancelled is True
    assert result.error == "Command cancelled."
    assert time.monotonic() - started < 5


def test_local_direct_caps_captured_output(tmp_path):
    shell, command = _python_command("print('x' * 2000)")
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)
    request.max_stdout = 40
    request.resources = {"max_output_bytes": 80}

    result = LocalDirectRunner().run(request)

    assert result.status == "ok"
    assert result.stdout.endswith("...[truncated]")
    assert len(result.stdout) < 100


def test_local_restricted_metadata_is_advisory(tmp_path):
    shell, command = _python_command("print('ok')")
    request = _request(command)
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)

    result = LocalRestrictedRunner().run(request)

    assert result.status == "ok"
    assert result.sandbox["backend"] == "local_restricted"
    assert result.sandbox["security_label"] == "advisory"
    assert result.sandbox["filesystem_isolation"] == "none"
    assert result.sandbox["network_isolation"] == "none"


def test_local_restricted_uses_sanitized_env(tmp_path):
    shell, command = _python_command("import os; print(os.getenv('MONAW_TEST_VALUE') or '')")
    request = _request(command, env={"MONAW_TEST_VALUE": "hello"})
    request.shell = shell  # type: ignore[assignment]
    request.workdir = str(tmp_path)

    result = LocalRestrictedRunner().run(request)

    assert result.status == "ok"
    assert result.stdout.strip() == "hello"
    assert result.env_keys == ["MONAW_TEST_VALUE"]
