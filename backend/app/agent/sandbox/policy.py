from __future__ import annotations

import re

from app.agent.sandbox.capabilities import probe_capabilities
from app.agent.sandbox.models import (
    SandboxBackend,
    SandboxCapabilities,
    SandboxDecision,
    SandboxMode,
    SandboxNetworkMode,
    SandboxProfile,
    SandboxRunRequest,
    SandboxWriteStrategy,
)
from app.agent.settings_store import SandboxSettings

_UNTRUSTED_PATTERNS = [
    r"\bcurl\b.*\|\s*(sh|bash|cmd|powershell|pwsh)\b",
    r"\bwget\b.*\|\s*(sh|bash)\b",
    r"\birm\b.*\|\s*iex\b",
    r"\bInvoke-WebRequest\b.*\|\s*Invoke-Expression\b",
    r"\biwr\b.*\|\s*iex\b",
    r"\bnpm\s+(install|ci)\b",
    r"\bpnpm\s+install\b",
    r"\byarn\s+install\b",
    r"\bpip(?:\d+(?:\.\d+)?)?\s+install\b",
    r"\bpipx\s+install\b",
    r"\buv\s+pip\s+install\b",
    r"\bpoetry\s+install\b",
    r"\bpython\s+setup\.py\b",
    r"\bgit\s+clone\b.*(&&|;|\|)",
]

_HOST_REQUIRED_PATTERNS = [
    r"\bGet-Process\b",
    r"\bGet-Service\b",
    r"\bStart-Process\b",
    r"\bStop-Service\b",
    r"\bNew-ScheduledTask\b",
    r"\bRegister-ScheduledTask\b",
    r"\bpywinauto\b",
]

_BLOCKED_PATTERNS = [
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\btakeown\b",
    r"\bicacls\b\s+[A-Za-z]:\\",
    r"\brm\s+-rf\s+/",
    r"\bRemove-Item\b(?=[\s\S]*\s-Recurse\b)(?=[\s\S]*\s-Force\b)(?=[\s\S]*[A-Za-z]:\\)",
    r"\breg\s+(add|delete|save|restore)\b",
    r"\bSet-ItemProperty\b.*Registry::",
]


def classify_command(command: str, *, default_profile: SandboxProfile = "standard") -> SandboxProfile:
    text = str(command or "")
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _BLOCKED_PATTERNS):
        return "blocked"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _UNTRUSTED_PATTERNS):
        return "untrusted"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _HOST_REQUIRED_PATTERNS):
        return "host_required"
    return default_profile


class SandboxPolicy:
    def __init__(
        self,
        settings: SandboxSettings,
        *,
        capabilities: SandboxCapabilities | None = None,
    ) -> None:
        self.settings = settings
        self.capabilities = capabilities or probe_capabilities(settings)

    def decide(self, request: SandboxRunRequest) -> SandboxDecision:
        profile = request.profile or classify_command(
            request.command,
            default_profile=self.settings.default_profile,
        )
        network = self._network_mode(request.network)
        write_strategy = request.write_strategy or self.settings.default_write_strategy

        if request.elevated:
            return self._blocked(profile, network, write_strategy, "Elevated execution is blocked", "elevated_blocked")
        if profile == "blocked":
            return self._blocked(profile, network, write_strategy, "Command matches a blocked sandbox pattern", "command_blocked")
        if not self.settings.enabled or self.settings.mode in {"off", "disabled"}:
            return SandboxDecision(
                allowed=True,
                required=False,
                profile=profile,
                backend="none",
                mode=self.settings.mode,
                security_label="none",
                network=network,
                network_enforcement="none",
                write_strategy=write_strategy,
                reason="Sandbox disabled",
            )

        backend_mode = request.requested_backend or self.settings.mode
        backend = self._select_backend(backend_mode, profile)
        if backend == "none":
            return SandboxDecision(
                allowed=True,
                required=False,
                profile=profile,
                backend="none",
                mode="off",
                security_label="none",
                network=network,
                network_enforcement="none",
                write_strategy=write_strategy,
                reason="Sandbox disabled for this run",
            )
        if backend is None:
            reason = "No available sandbox backend can satisfy this command"
            if profile == "untrusted" and self.settings.require_strong_for_untrusted:
                reason = "Strong sandbox required for untrusted command, but no strong backend is available"
            return self._blocked(profile, network, write_strategy, reason, "sandbox_backend_unavailable")

        capability = self.capabilities.for_backend(backend)
        if capability is None or not capability.available:
            if backend == "local_direct":
                return SandboxDecision(
                    allowed=True,
                    required=False,
                    profile=profile,
                    backend="local_direct",
                    mode=self.settings.mode,
                    security_label="none",
                    network=network,
                    network_enforcement="none",
                    write_strategy=write_strategy,
                    reason="Sandbox policy allowed local direct compatibility execution",
                )
            return self._blocked(
                profile,
                network,
                write_strategy,
                f"Sandbox backend {backend} is unavailable",
                "sandbox_backend_unavailable",
            )
        if profile == "untrusted" and self.settings.require_strong_for_untrusted and capability.security_label != "strong":
            return self._blocked(
                profile,
                network,
                write_strategy,
                "Strong sandbox required for untrusted command",
                "strong_sandbox_required",
            )
        if profile == "untrusted" and network == "deny" and capability.network_enforcement != "enforced":
            return self._blocked(
                profile,
                network,
                write_strategy,
                "Network deny cannot be enforced by the selected backend",
                "network_deny_not_enforced",
            )

        return SandboxDecision(
            allowed=True,
            required=True,
            profile=profile,
            backend=backend,
            mode=self.settings.mode,
            security_label=capability.security_label,
            network=network,
            network_enforcement=capability.network_enforcement,
            write_strategy=write_strategy,
            reason="Sandbox policy allowed the command",
        )

    def _select_backend(self, mode: SandboxMode, profile: SandboxProfile) -> SandboxBackend | None:
        if mode in {"off", "disabled"}:
            return "none"
        if mode in {"docker", "local_restricted", "wsl"}:
            capability = self.capabilities.for_backend(mode)
            return mode if capability and capability.available else None
        if mode == "enforce":
            capability = self.capabilities.for_backend("docker")
            return "docker" if capability and capability.available else None
        if profile == "untrusted":
            if self.capabilities.docker.available:
                return "docker"
            if self.settings.require_strong_for_untrusted:
                return None
            if self.capabilities.local_restricted.available:
                return "local_restricted"
            if self.capabilities.wsl.available:
                return "wsl"
            return "local_direct"
        if profile == "host_required":
            return "local_restricted" if self.capabilities.local_restricted.available else "local_direct"
        if self.capabilities.local_restricted.available:
            return "local_restricted"
        if self.capabilities.wsl.available:
            return "wsl"
        return "local_direct"

    def _network_mode(self, requested: SandboxNetworkMode | None) -> SandboxNetworkMode:
        if requested is not None:
            return requested
        return "allow" if self.settings.network.default == "allow" else "deny"

    def _blocked(
        self,
        profile: SandboxProfile,
        network: SandboxNetworkMode,
        write_strategy: SandboxWriteStrategy,
        reason: str,
        reason_code: str,
    ) -> SandboxDecision:
        return SandboxDecision(
            allowed=False,
            required=self.settings.enabled and self.settings.mode != "off",
            profile=profile,
            backend="none",
            mode=self.settings.mode,
            security_label="none",
            network=network,
            network_enforcement="none",
            write_strategy=write_strategy,
            reason=reason,
            reason_code=reason_code,
        )
