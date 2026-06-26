from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any

from app.agent.sandbox.models import SandboxBackendCapability, SandboxCapabilities, SandboxStatus
from app.agent.settings_store import SandboxSettings


def _run_probe(command: list[str], *, timeout: float = 3.0) -> tuple[bool, str, str]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "", "executable_not_found"
    except subprocess.TimeoutExpired:
        return False, "", "probe_timeout"
    except OSError as exc:
        return False, "", f"probe_failed:{exc.__class__.__name__}"

    output = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        reason = output.splitlines()[0] if output else f"exit_code_{completed.returncode}"
        return False, "", reason
    return True, output.splitlines()[0] if output else "", ""


def probe_docker(settings: SandboxSettings) -> SandboxBackendCapability:
    if not settings.docker.enabled:
        return SandboxBackendCapability(
            backend="docker",
            enabled=False,
            available=False,
            security_label="strong",
            network_enforcement="enforced",
            reason="disabled",
        )
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-fA-F]{64}", settings.docker.image.strip()):
        return SandboxBackendCapability(
            backend="docker",
            enabled=True,
            available=False,
            security_label="strong",
            network_enforcement="enforced",
            reason="docker_image_not_pinned",
        )
    if shutil.which("docker") is None:
        return SandboxBackendCapability(
            backend="docker",
            enabled=True,
            available=False,
            security_label="strong",
            network_enforcement="enforced",
            reason="docker_not_found",
        )

    available, version, reason = _run_probe(["docker", "version", "--format", "{{.Server.Version}}"])
    return SandboxBackendCapability(
        backend="docker",
        enabled=True,
        available=available,
        security_label="strong",
        network_enforcement="enforced",
        version=version,
        reason=reason,
    )


def probe_wsl(settings: SandboxSettings) -> SandboxBackendCapability:
    if not settings.wsl.enabled:
        return SandboxBackendCapability(
            backend="wsl",
            enabled=False,
            available=False,
            security_label="medium",
            network_enforcement="advisory",
            reason="disabled",
        )
    if shutil.which("wsl.exe") is None and shutil.which("wsl") is None:
        return SandboxBackendCapability(
            backend="wsl",
            enabled=True,
            available=False,
            security_label="medium",
            network_enforcement="advisory",
            reason="wsl_not_found",
        )

    command = ["wsl.exe", "--status"] if shutil.which("wsl.exe") else ["wsl", "--status"]
    available, version, reason = _run_probe(command)
    return SandboxBackendCapability(
        backend="wsl",
        enabled=True,
        available=available,
        security_label="medium",
        network_enforcement="advisory",
        version=version,
        reason=reason,
    )


def probe_local_restricted(settings: SandboxSettings) -> SandboxBackendCapability:
    if not settings.local_restricted.enabled:
        return SandboxBackendCapability(
            backend="local_restricted",
            enabled=False,
            available=False,
            security_label="advisory",
            network_enforcement="advisory",
            reason="disabled",
        )

    reason = "" if os.name == "nt" else "non_windows_compat_mode"
    return SandboxBackendCapability(
        backend="local_restricted",
        enabled=True,
        available=True,
        security_label="advisory",
        network_enforcement="advisory",
        reason=reason,
    )


def probe_capabilities(settings: SandboxSettings) -> SandboxCapabilities:
    return SandboxCapabilities(
        docker=probe_docker(settings),
        local_restricted=probe_local_restricted(settings),
        wsl=probe_wsl(settings),
    )


def get_sandbox_status(
    settings: SandboxSettings,
    *,
    capabilities: SandboxCapabilities | None = None,
) -> dict[str, Any]:
    probed = capabilities or probe_capabilities(settings)
    status = SandboxStatus(
        enabled=settings.enabled,
        mode=settings.mode,
        default_profile=settings.default_profile,
        default_network=settings.network.default,
        default_write_strategy=settings.default_write_strategy,
        require_strong_for_untrusted=settings.require_strong_for_untrusted,
        backends={
            "docker": probed.docker.model_dump(),
            "local_restricted": probed.local_restricted.model_dump(),
            "host": {
                "backend": "local_direct",
                "enabled": settings.enabled and settings.mode == "host",
                "available": True,
                "security_label": "none",
                "network_enforcement": "none",
                "version": "",
                "reason": "explicit_approval_required",
            },
            "wsl": probed.wsl.model_dump(),
        },
    )
    return status.model_dump()
