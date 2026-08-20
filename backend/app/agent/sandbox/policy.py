from __future__ import annotations

import os
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
    r"(?:\bformat\.com\b|\bformat\s+(?:/fs:\w+\s+)?[A-Za-z]:|\bFormat-Volume\b|\bdiskpart\b)",
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
        trust_class = "untrusted" if profile == "untrusted" else "trusted"

        if request.elevated:
            return self._blocked(profile, network, write_strategy, "Elevated execution is blocked", "elevated_blocked")
        if profile == "blocked":
            return self._blocked(profile, network, write_strategy, "Command matches a blocked sandbox pattern", "command_blocked")
        if not self.settings.enabled or self.settings.mode in {"off", "disabled"}:
            return self._blocked(
                profile,
                network,
                write_strategy,
                "Shell execution is disabled by sandbox policy",
                "shell_execution_disabled",
            )

        backend_mode = request.requested_backend or self.settings.mode
        backend = self._select_backend(backend_mode, profile)
        if backend is None:
            reason = "No strong sandbox backend is available for this command"
            if profile == "host_required":
                reason = "Host-required commands need explicit host mode"
            return self._blocked(
                profile,
                network,
                write_strategy,
                reason,
                "sandbox_backend_unavailable",
                requested_shell=request.shell,
                effective_shell=self._effective_shell(request, None, profile),
            )

        effective_shell = self._effective_shell(request, backend, profile)
        shell_fallback = False
        if backend == "docker" and effective_shell != "bash":
            # Docker's pinned image has a bash entrypoint. An automatic
            # request chooses bash when possible; an explicit Windows shell
            # is routed to the advisory host runner with a visible approval
            # requirement instead of silently executing on the host.
            shell_fallback = True
            backend = (
                "local_restricted"
                if self.capabilities.local_restricted.available
                else "local_direct"
            )

        if backend == "local_direct":
            return SandboxDecision(
                allowed=True,
                required=False,
                trust_class=trust_class,
                required_isolation="none",
                profile=profile,
                backend=backend,
                mode=self.settings.mode,
                security_label="none",
                network=network,
                network_enforcement="none",
                write_strategy=write_strategy,
                requested_shell=request.shell,
                effective_shell=effective_shell,
                filesystem_policy="host",
                explicit_approval_required=True,
                reason=(
                    "Docker supports bash only; this command will run directly on the host "
                    "after explicit approval"
                    if shell_fallback
                    else "This command will run directly on the host without isolation"
                ),
                reason_code=(
                    "docker_shell_fallback_requires_approval"
                    if shell_fallback
                    else "host_execution_approval_required"
                ),
            )

        capability = self.capabilities.for_backend(backend)
        if capability is None or not capability.available:
            return self._blocked(
                profile,
                network,
                write_strategy,
                f"Execution backend {backend} is unavailable",
                "sandbox_backend_unavailable",
            )
        if backend not in {"local_restricted"} and capability.security_label != "strong":
            return self._blocked(
                profile,
                network,
                write_strategy,
                "Strong sandbox isolation is required",
                "strong_sandbox_required",
            )
        if backend == "local_restricted":
            return SandboxDecision(
                allowed=True,
                required=False,
                trust_class=trust_class,
                required_isolation="none",
                profile=profile,
                backend=backend,
                mode=self.settings.mode,
                security_label="advisory",
                network=network,
                network_enforcement="advisory",
                write_strategy=write_strategy,
                requested_shell=request.shell,
                effective_shell=effective_shell,
                filesystem_policy="host",
                explicit_approval_required=True,
                reason=(
                    "Docker supports bash only; this command will use the advisory host runner "
                    "after explicit approval"
                    if shell_fallback
                    else "This command will use the advisory host runner"
                ),
                reason_code=(
                    "docker_shell_fallback_requires_approval"
                    if shell_fallback
                    else "host_execution_approval_required"
                ),
            )
        if network == "deny" and capability.network_enforcement != "enforced":
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
            trust_class=trust_class,
            required_isolation="strong",
            profile=profile,
            backend=backend,
            mode=self.settings.mode,
            security_label=capability.security_label,
            network=network,
            network_enforcement=capability.network_enforcement,
            write_strategy=write_strategy,
            requested_shell=request.shell,
            effective_shell=effective_shell,
            filesystem_policy="container",
            explicit_approval_required=False,
            reason="Strong sandbox policy allowed the command",
        )

    def _effective_shell(
        self,
        request: SandboxRunRequest,
        backend: SandboxBackend | None,
        profile: SandboxProfile,
    ) -> str:
        requested = str(request.shell or "auto").lower()
        if requested != "auto":
            return requested
        if profile == "host_required":
            return "powershell" if os.name == "nt" else "bash"
        if backend == "docker":
            return "powershell" if self._looks_like_powershell(request.command) else "bash"
        return "powershell" if os.name == "nt" else "bash"

    @staticmethod
    def _looks_like_powershell(command: str) -> bool:
        text = str(command or "")
        return bool(
            re.search(
                r"(?:\b(?:Get|Set|New|Remove|Write|Where|ForEach|Format|Invoke|Start|Stop|Test|Convert|Select|Out|Measure|Sort|Export|Import)-[A-Za-z]+\b|(?:^|[\s;|])\$[A-Za-z_][A-Za-z0-9_]*|`|\b[A-Za-z]:[\\/]|\s-(?:Recurse|Force|NoProfile|ExecutionPolicy)\b)",
                text,
                re.IGNORECASE,
            )
        )

    def _select_backend(self, mode: SandboxMode, profile: SandboxProfile) -> SandboxBackend | None:
        if mode in {"off", "disabled"}:
            return None
        if mode == "host":
            return "local_restricted" if self.capabilities.local_restricted.available else "local_direct"
        if mode == "local_restricted":
            return "local_restricted" if self.capabilities.local_restricted.available else None
        if mode == "docker":
            capability = self.capabilities.for_backend(mode)
            return mode if capability and capability.available else None
        if mode == "enforce":
            return "docker" if self.capabilities.docker.available else None
        if profile == "untrusted":
            if self.capabilities.docker.available:
                return "docker"
            if mode in {"docker", "enforce"}:
                return None
            return "local_restricted" if self.capabilities.local_restricted.available else "local_direct"
        if profile == "host_required":
            return "local_restricted" if self.capabilities.local_restricted.available else "local_direct"
        if self.capabilities.docker.available:
            return "docker"
        return "local_restricted" if self.capabilities.local_restricted.available else "local_direct"

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
        *,
        requested_shell: str = "auto",
        effective_shell: str = "powershell",
    ) -> SandboxDecision:
        return SandboxDecision(
            allowed=False,
            required=self.settings.enabled and self.settings.mode not in {"off", "disabled", "host"},
            trust_class=("blocked" if profile == "blocked" else "untrusted" if profile == "untrusted" else "trusted"),
            required_isolation="strong" if self.settings.mode not in {"off", "disabled", "host"} else "none",
            profile=profile,
            backend="none",
            mode=self.settings.mode,
            security_label="none",
            network=network,
            network_enforcement="none",
            write_strategy=write_strategy,
            requested_shell=requested_shell,
            effective_shell=effective_shell,
            filesystem_policy="none",
            reason=reason,
            reason_code=reason_code,
        )
