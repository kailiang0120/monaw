from __future__ import annotations

from typing import Any

from app.agent.sandbox.capabilities import probe_capabilities
from app.agent.sandbox.backends.docker import DockerRunner
from app.agent.sandbox.backends.local_direct import LocalDirectRunner
from app.agent.sandbox.backends.local_restricted import LocalRestrictedRunner
from app.agent.sandbox.models import SandboxCapabilities
from app.agent.sandbox.models import SandboxDecision, SandboxExecutionRequest, SandboxExecutionResult, SandboxRunRequest
from app.agent.sandbox.policy import SandboxPolicy
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

    def run(
        self,
        request: SandboxExecutionRequest,
        *,
        decision: SandboxDecision | None = None,
    ) -> SandboxExecutionResult:
        # Recheck the actual request under current settings. A supplied preview
        # is not authorization to bypass a changed mode or a different command.
        decision = SandboxPolicy(self.settings, capabilities=self.capabilities).decide(
            SandboxRunRequest(
                command=request.command,
                shell=request.shell,
                workdir=request.workdir,
                env=request.env,
                timeout=request.timeout,
                profile=request.profile,
                requested_backend=None,
                network=request.network,
                write_strategy=str(request.copy_policy.get("write_strategy") or "") or None,
            )
        )
        selected_backend = decision.backend if decision.allowed else "none"
        if not decision.allowed:
            return self._blocked_result(request, backend=selected_backend,
                reason=decision.reason, reason_code=decision.reason_code)
        if request.backend and request.backend != selected_backend:
            return self._blocked_result(
                request,
                backend=selected_backend,
                reason=(
                    f"Requested backend '{request.backend}' does not match the sandbox policy "
                    f"decision '{selected_backend}'."
                ),
                reason_code="sandbox_backend_mismatch",
            )
        request.timeout = min(max(1, request.timeout), self.settings.resources.timeout_seconds)
        request.resources = self.settings.resources.model_dump()
        request.network = decision.network
        request.profile = decision.profile
        if selected_backend == "local_direct":
            return self.local_direct.run(request)
        if selected_backend == "docker":
            if self.capabilities.docker.available and self.docker.is_available():
                return self.docker.run(request)
            return self._blocked_result(
                request,
                backend=selected_backend,
                reason="Docker sandbox backend is unavailable. Start Docker, or choose auto/host mode.",
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
                "Use auto/host mode for compatibility or enable a supported strong backend when available."
            ),
            reason_code="sandbox_backend_unavailable",
        )

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
