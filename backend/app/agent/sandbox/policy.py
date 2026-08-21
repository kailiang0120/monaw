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

_CONTAINED_UNTRUSTED_PATTERNS = [
    r"\b(?:curl|wget|irm|iwr|Invoke-WebRequest)\b.*\|\s*(?:sh|bash|cmd|powershell|pwsh|iex|Invoke-Expression)\b",
    r"\bgit\s+clone\b.*(?:&&|;|\|)",
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

_CONTAINER_SAFE_EXECUTABLES = frozenset(
    {
        "bash",
        "basename",
        "cat",
        "cd",
        "chmod",
        "chown",
        "cp",
        "cut",
        "date",
        "dirname",
        "du",
        "echo",
        "false",
        "find",
        "grep",
        "head",
        "id",
        "kill",
        "ln",
        "ls",
        "mkdir",
        "mktemp",
        "mv",
        "od",
        "pip",
        "pip3",
        "printf",
        "pwd",
        "python",
        "python3",
        "readlink",
        "realpath",
        "rm",
        "rmdir",
        "sed",
        "sh",
        "sleep",
        "sort",
        "stat",
        "tail",
        "tee",
        "test",
        "touch",
        "tr",
        "true",
        "uname",
        "uniq",
        "wc",
    }
)
_PYTHON_EXECUTABLES = frozenset(
    {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
)

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
        host_tool_fallback = bool(
            backend == "docker"
            and str(request.shell or "auto").lower() == "auto"
            and effective_shell == "bash"
            and self._requires_host_runner(request.command)
            and not (
                profile == "untrusted"
                and self._requires_contained_untrusted_command(request.command)
            )
        )
        if backend == "docker" and effective_shell != "bash":
            shell_fallback = True
        if shell_fallback or host_tool_fallback:
            # Docker's pinned image has a bash entrypoint. An automatic
            # request chooses bash when possible. Commands that need an
            # explicit Windows shell or a host-only developer tool are routed
            # to the advisory host runner with a visible approval requirement
            # instead of silently running in the wrong environment.
            backend = (
                "local_restricted"
                if self.capabilities.local_restricted.available
                else "local_direct"
            )
            effective_shell = self._effective_shell(request, backend, profile)

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
                    self._host_fallback_reason(request.command, runner="direct")
                    if host_tool_fallback
                    else "Docker supports bash only; this command will run directly on the host "
                    "after explicit approval"
                    if shell_fallback
                    else "This command will run directly on the host without isolation"
                ),
                reason_code=(
                    "docker_host_tool_fallback_requires_approval"
                    if host_tool_fallback
                    else "docker_shell_fallback_requires_approval"
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
                    self._host_fallback_reason(request.command, runner="advisory")
                    if host_tool_fallback
                    else "Docker supports bash only; this command will use the advisory host runner "
                    "after explicit approval"
                    if shell_fallback
                    else "This command will use the advisory host runner"
                ),
                reason_code=(
                    "docker_host_tool_fallback_requires_approval"
                    if host_tool_fallback
                    else "docker_shell_fallback_requires_approval"
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

    @staticmethod
    def _requires_host_runner(command: str) -> bool:
        text = str(command or "")
        if not text.strip():
            return False
        # Command substitutions can invoke an arbitrary executable without
        # appearing at a shell segment boundary. Keep those on the host
        # unless the user explicitly requested bash/Docker.
        if "$(" in text or "`" in text:
            return True
        for segment in SandboxPolicy._split_shell_segments(text):
            executable = SandboxPolicy._segment_executable(segment)
            if not executable:
                return True
            if executable in _PYTHON_EXECUTABLES and SandboxPolicy._python_script_argument(segment):
                return True
            if executable in _PYTHON_EXECUTABLES and re.search(
                r"\b(?:subprocess|os\.system|Popen|check_call|check_output|import|from)\b",
                segment,
                re.IGNORECASE,
            ):
                return True
            if executable in {"pip", "pip3"} and re.search(
                r"(?<!\S)install(?=\s|$)", segment, re.IGNORECASE
            ):
                return True
            if executable in {"bash", "sh"}:
                nested = re.search(r"(?:^|\s)-{1,2}(?:l?c|command)\s+(.+)$", segment, re.IGNORECASE)
                if nested:
                    inner = nested.group(1).strip()
                    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in {'"', "'"}:
                        inner = inner[1:-1]
                    if SandboxPolicy._requires_host_runner(inner):
                        return True
            if executable not in _CONTAINER_SAFE_EXECUTABLES:
                return True
            if executable in {"find", "which"} and re.search(
                r"(?<!\S)(?:-exec|--exec|xargs)(?=\s|$)", segment, re.IGNORECASE
            ):
                return True
        return False

    @staticmethod
    def _requires_contained_untrusted_command(command: str) -> bool:
        text = str(command or "")
        return any(
            re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            for pattern in _CONTAINED_UNTRUSTED_PATTERNS
        )

    @staticmethod
    def _host_tool_name(command: str) -> str:
        for segment in SandboxPolicy._split_shell_segments(str(command or "")):
            executable = SandboxPolicy._segment_executable(segment)
            if not executable:
                continue
            if executable in _PYTHON_EXECUTABLES and SandboxPolicy._python_script_argument(segment):
                return executable
            if executable in {"pip", "pip3"} and re.search(
                r"(?<!\S)install(?=\s|$)", segment, re.IGNORECASE
            ):
                return executable
            if executable not in _CONTAINER_SAFE_EXECUTABLES:
                return executable
        if re.search(r"\b(?:python|python3)\s+-m\s+pytest\b", command, re.IGNORECASE):
            return "pytest"
        if re.search(r"\b(?:python|python3)\s+-m\s+pip\b", command, re.IGNORECASE):
            return "pip"
        return "host tool"

    @staticmethod
    def _host_fallback_reason(command: str, *, runner: str) -> str:
        suffix = (
            "will run directly on the host"
            if runner == "direct"
            else "will use the advisory host runner"
        )
        for segment in SandboxPolicy._split_shell_segments(str(command or "")):
            executable = SandboxPolicy._segment_executable(segment)
            if executable in _PYTHON_EXECUTABLES and SandboxPolicy._python_script_argument(segment):
                return f"Python script execution uses the host interpreter; this command {suffix} after explicit approval"
            if executable in _PYTHON_EXECUTABLES and re.search(
                r"\b(?:subprocess|os\.system|Popen|check_call|check_output|import|from)\b",
                segment,
                re.IGNORECASE,
            ):
                return f"Python code with host dependencies uses the host interpreter; this command {suffix} after explicit approval"
        if re.search(
            r"\b(?:pip|pip3)\s+install\b|\b(?:uv\s+pip|poetry)\s+install\b",
            str(command or ""),
            re.IGNORECASE,
        ):
            return f"Package installation is host-specific; this command {suffix} after explicit approval"
        tool = SandboxPolicy._host_tool_name(command)
        return (
            f"The command uses '{tool}', which is not in the pinned Docker image's safe command allowlist; "
            f"this command {suffix} after explicit approval"
        )

    @staticmethod
    def _split_shell_segments(command: str) -> list[str]:
        segments: list[str] = []
        buffer: list[str] = []
        quote = ""
        index = 0
        text = str(command or "")
        while index < len(text):
            char = text[index]
            if quote:
                buffer.append(char)
                if char == quote:
                    quote = ""
                elif char == "\\" and quote == '"' and index + 1 < len(text):
                    index += 1
                    buffer.append(text[index])
                index += 1
                continue
            if char in {'"', "'"}:
                quote = char
                buffer.append(char)
                index += 1
                continue
            if char == "\\" and index + 1 < len(text):
                buffer.extend((char, text[index + 1]))
                index += 2
                continue
            if text.startswith("&&", index) or text.startswith("||", index):
                if "".join(buffer).strip():
                    segments.append("".join(buffer).strip())
                buffer = []
                index += 2
                continue
            if char in ";|&()\n":
                if "".join(buffer).strip():
                    segments.append("".join(buffer).strip())
                buffer = []
                index += 1
                continue
            buffer.append(char)
            index += 1
        if "".join(buffer).strip():
            segments.append("".join(buffer).strip())
        return segments

    @staticmethod
    def _segment_executable(segment: str) -> str:
        text = str(segment or "").strip()
        while True:
            assignment = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S+)\s+", text)
            if assignment is None:
                break
            text = text[assignment.end():].lstrip()
        match = re.match(r"[!\s]*([A-Za-z0-9_./\\-]+)", text)
        if match is None:
            return ""
        return match.group(1).replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _python_script_argument(segment: str) -> bool:
        executable = SandboxPolicy._segment_executable(segment)
        if executable not in _PYTHON_EXECUTABLES:
            return False
        raw = str(segment or "").strip()
        executable_match = re.match(r"[!\s]*[A-Za-z0-9_./\\-]+", raw)
        remainder = raw[executable_match.end():].strip() if executable_match else ""
        tokens = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", remainder)
        for token in tokens:
            normalized = token.strip("\"'")
            if normalized in {"-c", "--command", "-"}:
                return False
            if normalized in {"-m", "--module"}:
                return True
            if normalized.startswith("-"):
                continue
            return True
        return False

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
