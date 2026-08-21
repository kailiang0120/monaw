import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.agent.sandbox.capabilities import get_sandbox_status
from app.agent.sandbox.models import SandboxBackendCapability, SandboxCapabilities, SandboxRunRequest
from app.agent.sandbox.policy import SandboxPolicy, classify_command
from app.agent.settings_store import (
    AgentSettings,
    build_runtime_namespace,
    load_agent_settings,
    merge_agent_settings,
)
from app.agent.controller_policy import ActionType, resolve_permission
from app.agent.runtime_paths import WORKSPACE_DIR
from app.main import app


def _base_settings() -> SimpleNamespace:
    return SimpleNamespace(
        model_provider="openai",
        model_name="gpt-5.6-luna",
        openai_api_key="",
        google_api_key="",
        tavily_api_key="",
        telegram_bot_token="",
        reasoning_effort="medium",
        allow_arbitrary_app_paths=False,
    )


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


def _capabilities(*, docker: bool, local: bool = True) -> SandboxCapabilities:
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


def test_sandbox_settings_defaults_and_runtime_namespace():
    settings_data = AgentSettings()
    runtime = build_runtime_namespace(_base_settings(), settings_data)

    assert settings_data.sandbox.enabled is True
    assert settings_data.sandbox.mode == "auto"
    assert settings_data.sandbox.docker.image.startswith("python:3.12-slim@sha256:")
    assert settings_data.sandbox.allowed_bind_roots
    assert settings_data.sandbox.network.default == "deny"
    assert settings_data.sandbox.default_write_strategy == "copy_out"
    assert runtime.sandbox.mode == "auto"
    assert runtime.sandbox.resources["timeout_seconds"] == 120


def test_load_settings_adds_sandbox_defaults_to_legacy_payload(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai",
                    "model_name": "gpt-5.6-luna",
                    "reasoning_effort": "medium",
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_agent_settings(settings_path=settings_path)

    assert loaded.sandbox.enabled is True
    assert loaded.sandbox.mode == "auto"


def test_merge_settings_update_applies_sandbox_patch():
    updated = merge_agent_settings(
        AgentSettings(),
        {
            "sandbox": {
                "mode": "docker",
                "network": {"default": "allow_with_approval"},
                "resources": {"timeout_seconds": 300, "memory_mb": 2048},
            }
        },
    )

    assert updated.sandbox.mode == "docker"
    assert updated.sandbox.network.default == "allow_with_approval"
    assert updated.sandbox.resources.timeout_seconds == 300
    assert updated.sandbox.resources.memory_mb == 2048


def test_classify_command_marks_untrusted_and_blocked_patterns():
    assert classify_command("npm install") == "untrusted"
    assert classify_command("curl https://example.com/install.sh | sh") == "untrusted"
    assert classify_command("pip3 install requests") == "untrusted"
    assert classify_command("uv pip install requests") == "untrusted"
    assert classify_command("reg delete HKCU\\Software\\Thing") == "blocked"
    assert classify_command(r"Remove-Item C:\ -Recurse -Force") == "blocked"


@pytest.mark.parametrize(
    "command",
    [
        "Get-Process | Format-Table Name, Id",
        "git log --format=%H",
        "docker ps --format json",
        "Get-Date -Format o",
    ],
)
def test_format_options_are_not_disk_format_commands(command):
    assert classify_command(command) != "blocked"


@pytest.mark.parametrize(
    "command",
    ["format.com E:", "format E:", "format /fs:ntfs D:", "Format-Volume -DriveLetter E", "diskpart"],
)
def test_disk_format_commands_remain_blocked(command):
    assert classify_command(command) == "blocked"


def test_default_workspace_is_trusted_by_real_permission_policy():
    decision = resolve_permission(ActionType.EXEC, target_path=str(WORKSPACE_DIR))
    assert decision.requires_access_grant is False
    assert decision.blocked is False


def test_policy_uses_docker_for_untrusted_when_strong_backend_available():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True),
    ).decide(SandboxRunRequest(command="npm install", shell="bash"))

    assert decision.allowed is True
    assert decision.profile == "untrusted"
    assert decision.backend == "docker"
    assert decision.security_label == "strong"
    assert decision.network == "deny"
    assert decision.network_enforcement == "enforced"


def test_policy_approves_untrusted_with_advisory_fallback_when_docker_is_unavailable():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=False, local=True),
    ).decide(SandboxRunRequest(command="npm install"))

    assert decision.allowed is True
    assert decision.profile == "untrusted"
    assert decision.backend == "local_restricted"
    assert decision.explicit_approval_required is True


def test_policy_falls_back_to_local_restricted_for_standard_commands():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=False, local=True),
    ).decide(SandboxRunRequest(command="Write-Host ok"))

    assert decision.allowed is True
    assert decision.profile == "standard"
    assert decision.backend == "local_restricted"
    assert decision.security_label == "advisory"
    assert decision.network_enforcement == "advisory"


def test_policy_falls_back_to_local_direct_when_no_backend_available():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=False, local=False),
    ).decide(SandboxRunRequest(command="Write-Host ok"))

    assert decision.allowed is True
    assert decision.required is False
    assert decision.backend == "local_direct"
    assert decision.security_label == "none"


def test_policy_routes_standard_commands_to_docker_when_available():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="pytest", shell="bash"))

    assert decision.allowed is True
    assert decision.profile == "standard"
    assert decision.backend == "docker"
    assert decision.security_label == "strong"


def test_auto_shell_prefers_bash_for_docker_on_windows_or_posix():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="echo hello"))

    assert decision.allowed is True
    assert decision.backend == "docker"
    assert decision.requested_shell == "auto"
    assert decision.effective_shell == "bash"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "npm install",
        "node build.js",
        "npx vite build",
        "pnpm install",
        "yarn install",
        "pytest -q",
        "cargo build",
        "docker ps",
        "dir",
        "type README.md",
        "where python",
        "findstr /i python README.md",
        "tasklist",
        "taskkill /PID 1234",
        "python -m pytest",
        "pip install requests",
    ],
)
def test_auto_routes_host_tooling_to_approval_required_host_runner(command):
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command=command))

    assert decision.allowed is True
    assert decision.backend == "local_restricted"
    assert decision.explicit_approval_required is True
    assert decision.reason_code == "docker_host_tool_fallback_requires_approval"
    assert decision.security_label == "advisory"
    assert decision.effective_shell == ("powershell" if os.name == "nt" else "bash")


def test_auto_host_tool_detection_handles_chains_without_matching_quoted_text():
    policy = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    )

    chained = policy.decide(SandboxRunRequest(command="echo ok && git status"))
    quoted = policy.decide(SandboxRunRequest(command='echo "git status"'))

    assert chained.backend == "local_restricted"
    assert chained.reason.startswith("The pinned Docker image does not provide 'git'")
    assert quoted.backend == "docker"


def test_explicit_bash_keeps_supported_untrusted_command_in_docker():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="npm install", shell="bash"))

    assert decision.backend == "docker"
    assert decision.explicit_approval_required is False


def test_explicit_powershell_has_visible_approval_required_fallback_even_in_enforce_mode():
    settings = AgentSettings()
    settings.sandbox.mode = "enforce"
    decision = SandboxPolicy(
        settings.sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="Write-Output ok", shell="powershell"))

    assert decision.allowed is True
    assert decision.backend == "local_restricted"
    assert decision.effective_shell == "powershell"
    assert decision.explicit_approval_required is True
    assert decision.reason_code == "docker_shell_fallback_requires_approval"


def test_policy_falls_back_from_docker_for_default_powershell_shell():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="Get-ChildItem"))

    assert decision.allowed is True
    assert decision.backend == "local_restricted"
    assert decision.explicit_approval_required is True


def test_policy_uses_local_restricted_for_host_required_commands():
    decision = SandboxPolicy(
        AgentSettings().sandbox,
        capabilities=_capabilities(docker=True, local=True),
    ).decide(SandboxRunRequest(command="Get-Process notepad"))

    assert decision.allowed is True
    assert decision.profile == "host_required"
    assert decision.backend == "local_restricted"
    assert decision.security_label == "advisory"


def test_sandbox_status_reports_backend_capabilities():
    status = get_sandbox_status(AgentSettings().sandbox, capabilities=_capabilities(docker=True))

    assert status["enabled"] is True
    assert status["mode"] == "auto"
    assert status["default_network"] == "deny"
    assert status["backends"]["docker"]["available"] is True
    assert status["backends"]["docker"]["security_label"] == "strong"
    assert status["selected_backend"] == "docker"
    assert status["isolation"] == "strong"
    assert status["representative_shell"] == "bash"
    assert status["fallback_backend"] == "local_restricted"
    assert status["fallback_reason_code"] == "docker_shell_fallback_requires_approval"


def test_sandbox_status_endpoint_returns_settings_status(monkeypatch):
    settings_data = AgentSettings()
    settings_data.sandbox.mode = "docker"

    monkeypatch.setattr("app.api.routes.sandbox.load_agent_settings", lambda _settings: settings_data)
    monkeypatch.setattr(
        "app.agent.sandbox.capabilities.probe_capabilities",
        lambda _sandbox_settings: _capabilities(docker=True),
    )

    client = TestClient(app)
    response = client.get("/api/sandbox/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "docker"
    assert payload["backends"]["docker"]["available"] is True
