from __future__ import annotations

from typing import Any

from app.agent.sandbox.capabilities import probe_capabilities
from app.agent.sandbox.backends.docker import DockerRunner
from app.agent.sandbox.backends.local_direct import LocalDirectRunner
from app.agent.sandbox.backends.local_restricted import LocalRestrictedRunner
from app.agent.sandbox.models import SandboxCapabilities
from app.agent.sandbox.models import SandboxExecutionRequest, SandboxExecutionResult
from app.agent.settings_store import SandboxSettings


def coerce_sandbox_settings(settings: Any | None) -> SandboxSettings:
    if isinstance(settings, SandboxSettings):
        return settings
    if settings is None:
        return SandboxSettings()
    if hasattr(settings, "model_dump"):
        return SandboxSettings.model_validate(settings.model_dump())
    if isinstance(settings, dict):
        return SandboxSettings.model_validate(settings)
    values = {
        key: getattr(settings, key)
        for key in SandboxSettings.model_fields
        if hasattr(settings, key)
    }
    return SandboxSettings.model_validate(values)


class SandboxManager:
    def __init__(
        self,
        settings: SandboxSettings | Any | None = None,
        *,
        capabilities: SandboxCapabilities | None = None,
    ) -> None:
        self.settings = coerce_sandbox_settings(settings)
        self.capabilities = capabilities or probe_capabilities(self.settings)
        self.docker = DockerRunner(self.settings)
        self.local_direct = LocalDirectRunner()
        self.local_restricted = LocalRestrictedRunner()

    def run(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        selected_backend = self._select_backend(request)
        if selected_backend == "local_direct":
            return self.local_direct.run(request)
        if selected_backend == "docker":
            if self.capabilities.docker.available and self.docker.is_available():
                return self.docker.run(request)
            return self._blocked_result(
                request,
                backend=selected_backend,
                reason="Docker sandbox backend is unavailable. Install and start Docker, or choose auto/disabled mode.",
                reason_code="sandbox_backend_unavailable",
            )
        if selected_backend == "local_restricted":
            if self.capabilities.local_restricted.available and self.local_restricted.is_available():
                return self.local_restricted.run(request)
            return self._blocked_result(
                request,
                backend=selected_backend,
                reason="The local restricted advisory backend is unavailable on this host.",
                reason_code="sandbox_backend_unavailable",
            )
        return self._blocked_result(
            request,
            backend=selected_backend,
            reason=(
                "No implemented strong sandbox backend is available for this command. "
                "Use disabled/auto mode for compatibility or enable a supported strong backend when available."
            ),
            reason_code="sandbox_backend_unavailable",
        )

    def _select_backend(self, request: SandboxExecutionRequest) -> str:
        requested_backend = (request.backend or "").strip()
        if requested_backend:
            return requested_backend
        if not self.settings.enabled:
            return "local_direct"
        mode = str(self.settings.mode or "auto")
        if mode in {"off", "disabled"}:
            return "local_direct"
        if mode == "auto":
            if self.capabilities.local_restricted.available:
                return "local_restricted"
            return "local_direct"
        if mode == "enforce":
            return "docker" if self.capabilities.docker.available else "unavailable"
        return mode

    def _blocked_result(
        self,
        request: SandboxExecutionRequest,
        *,
        backend: str,
        reason: str,
        reason_code: str,
    ) -> SandboxExecutionResult:
        sandbox = {
            **request.env_metadata,
            "backend": backend,
            "selected_backend": backend,
            "security_label": "none",
            "filesystem_isolation": "none",
            "process_isolation": "none",
            "network_isolation": "none",
            "profile": request.profile,
            "mode": str(self.settings.mode or "auto"),
            "network": request.network,
            "workdir": request.workdir,
        }
        return SandboxExecutionResult(
            status="blocked",
            shell=request.shell,
            workdir=request.workdir,
            env_keys=list(request.env_metadata.get("explicit_env_keys", [])),
            sandbox=sandbox,
            reason=reason,
            reason_code=reason_code,
        )
