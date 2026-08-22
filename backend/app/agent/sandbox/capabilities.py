from __future__ import annotations

import os
import re
import shutil
import subprocess
import json
import threading
import time
from collections import OrderedDict
from typing import Any

from app.agent.sandbox.models import (
    SandboxBackendCapability,
    SandboxCapabilities,
    SandboxRunRequest,
    SandboxStatus,
)
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


_CAPABILITY_CACHE_TTL_SECONDS = 30.0
_CAPABILITY_CACHE_MAX_ENTRIES = 8
_capability_cache: OrderedDict[str, tuple[float, SandboxCapabilities]] = OrderedDict()
_capability_cache_lock = threading.RLock()


def _cache_key(settings: SandboxSettings) -> str:
    return json.dumps(settings.model_dump(), sort_keys=True, default=str)


def invalidate_capability_cache() -> None:
    with _capability_cache_lock:
        _capability_cache.clear()


def probe_capabilities(settings: SandboxSettings) -> SandboxCapabilities:
    key = _cache_key(settings)
    now = time.monotonic()
    with _capability_cache_lock:
        cached = _capability_cache.get(key)
        if cached is not None and now - cached[0] < _CAPABILITY_CACHE_TTL_SECONDS:
            _capability_cache.move_to_end(key)
            return cached[1]
        if cached is not None:
            _capability_cache.pop(key, None)

    probed = SandboxCapabilities(
        docker=probe_docker(settings),
        local_restricted=probe_local_restricted(settings),
    )
    with _capability_cache_lock:
        _capability_cache[key] = (now, probed)
        _capability_cache.move_to_end(key)
        while len(_capability_cache) > _CAPABILITY_CACHE_MAX_ENTRIES:
            _capability_cache.popitem(last=False)
    return probed


# The representative status probe uses an allowlisted coreutil, so it reports
# Docker regardless of whether Python imports can run there. Say so explicitly.
_PYTHON_IMPORT_SUPPORT_DETAIL = {
    "probed": "Python imports are checked against a probe of this exact pinned image.",
    "builtin_fallback": (
        "Python imports are checked against the built-in python:3.12-slim module list. "
        "Use Resolve & pull to verify this exact image."
    ),
    "unavailable": (
        "No module inventory for this image, so Python imports are routed to the "
        "approval-required host runner. Use Resolve & pull to probe the image."
    ),
}


def get_sandbox_status(
    settings: SandboxSettings,
    *,
    capabilities: SandboxCapabilities | None = None,
) -> dict[str, Any]:
    probed = capabilities or probe_capabilities(settings)
    from app.agent.sandbox.policy import SandboxPolicy

    policy = SandboxPolicy(settings, capabilities=probed)
    probe_workdir = (settings.allowed_bind_roots or [""])[0]
    representative = policy.decide(
        SandboxRunRequest(
            command="printf 'monaw sandbox status probe'",
            shell="auto",
            workdir=probe_workdir,
            profile=settings.default_profile,
            network=None,
            write_strategy=settings.default_write_strategy,
        )
    )
    powershell_fallback = policy.decide(
        SandboxRunRequest(
            command="Write-Output 'monaw PowerShell fallback probe'",
            shell="powershell",
            workdir=probe_workdir,
            profile="standard",
            network=None,
            write_strategy=settings.default_write_strategy,
        )
    )

    def _status_backend(decision) -> str:
        return decision.backend if decision.allowed else "none"

    status = SandboxStatus(
        enabled=settings.enabled,
        mode=settings.mode,
        default_profile=settings.default_profile,
        default_network=settings.network.default,
        default_write_strategy=settings.default_write_strategy,
        selected_backend=_status_backend(representative),
        isolation=representative.security_label,
        reason_code=representative.reason_code,
        reason=representative.reason,
        representative_shell=representative.effective_shell,
        representative_profile=representative.profile,
        fallback_backend=_status_backend(powershell_fallback),
        fallback_isolation=powershell_fallback.security_label,
        fallback_reason_code=powershell_fallback.reason_code,
        fallback_reason=powershell_fallback.reason,
        python_import_support=policy.python_import_support,
        python_import_detail=_PYTHON_IMPORT_SUPPORT_DETAIL.get(policy.python_import_support, ""),
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
        },
    )
    return status.model_dump()
