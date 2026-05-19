import json
import os
import shlex
import sys
from types import SimpleNamespace

from app.agent.sandbox.backends.local_direct import LocalDirectRunner
from app.agent.sandbox.backends.local_restricted import LocalRestrictedRunner
from app.agent.sandbox.environment import build_exec_environment
from app.agent.sandbox.manager import SandboxManager
from app.agent.sandbox.models import (
    SandboxBackendCapability,
    SandboxCapabilities,
    SandboxExecutionRequest,
    SandboxExecutionResult,
)
from app.agent.settings_store import AgentSettings
from app.skills.exec import tools as exec_tools


def _allow_exec(monkeypatch):
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


def _capabilities(*, docker: bool = False, local: bool = True, wsl: bool = False) -> SandboxCapabilities:
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
        wsl=_capability(
            "wsl",
            available=wsl,
            security_label="medium",
            network_enforcement="advisory",
            reason="" if wsl else "disabled",
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

        def run(self, request):
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


def test_exec_tool_blocks_untrusted_when_strong_backend_unavailable(monkeypatch, tmp_path):
    _allow_exec(monkeypatch)
    settings = AgentSettings()
    settings.sandbox.docker.enabled = False
    monkeypatch.setattr(exec_tools, "_ACTIVE_SETTINGS", SimpleNamespace(sandbox=settings.sandbox))

    result = json.loads(
        exec_tools.exec_tool("npm install", shell="bash", workdir=str(tmp_path), _bypass_gate=True)
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "sandbox_backend_unavailable"
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

        def run(self, request):
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


def test_auto_mode_uses_local_direct_when_no_real_backend_available():
    result = SandboxManager(
        AgentSettings().sandbox,
        capabilities=_capabilities(local=False),
    ).run(_request())

    assert result.status in {"ok", "error"}
    assert result.sandbox["backend"] == "local_direct"
    assert result.sandbox["security_label"] == "none"


def test_disabled_mode_uses_local_direct():
    settings = AgentSettings()
    settings.sandbox.mode = "disabled"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status in {"ok", "error"}
    assert result.sandbox["backend"] == "local_direct"


def test_enforce_mode_blocks_without_real_backend():
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "unavailable"


def test_requested_docker_backend_unavailable_before_implementation():
    settings = AgentSettings()
    settings.sandbox.mode = "docker"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities()).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "docker"


def test_auto_can_select_local_restricted_when_configured():
    result = SandboxManager(
        AgentSettings().sandbox,
        capabilities=_capabilities(local=True),
    ).run(_request())

    assert result.status in {"ok", "error"}
    assert result.sandbox["backend"] == "local_restricted"
    assert result.sandbox["security_label"] == "advisory"


def test_enforce_strong_does_not_accept_advisory_backend():
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"

    result = SandboxManager(settings.sandbox, capabilities=_capabilities(local=True)).run(_request())

    assert result.status == "blocked"
    assert result.reason_code == "sandbox_backend_unavailable"
    assert result.sandbox["selected_backend"] == "unavailable"


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
