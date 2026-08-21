from __future__ import annotations

import ast
import os
import re
import shutil

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
    r"\bpip(?:\d+(?:\.\d+)?)?\s+(?:download|install|lock|uninstall|wheel)\b",
    r"\bpip(?:\d+(?:\.\d+)?)?\s+(?:cache\s+(?:purge|remove)|config\s+(?:rename|set|unset))\b",
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
_PIP_EXECUTABLES = frozenset({"pip", "pip.exe", "pip3", "pip3.exe"})
_PIP_STATE_CHANGING_SUBCOMMANDS = frozenset({"download", "install", "lock", "uninstall", "wheel"})
_PIP_STATE_CHANGING_ACTIONS = {
    "cache": frozenset({"purge", "remove"}),
    "config": frozenset({"rename", "set", "unset"}),
}
_PIP_OPTIONS_WITH_VALUES = frozenset(
    {
        "-c",
        "-e",
        "-f",
        "-i",
        "-r",
        "--abi",
        "--build-constraint",
        "--cache-dir",
        "--cert",
        "--client-cert",
        "--config-file",
        "--constraint",
        "--config-settings",
        "--editable",
        "--extra-index-url",
        "--find-links",
        "--index-url",
        "--implementation",
        "--log",
        "--log-file",
        "--no-binary",
        "--only-binary",
        "--platform",
        "--proxy",
        "--python",
        "--python-version",
        "--retries",
        "--root",
        "--root-user-action",
        "--src",
        "--target",
        "--timeout",
        "--trusted-host",
    }
)
# This is the module set for the Python runtime in the default Docker image
# (python:3.12-slim). It must not come from sys.stdlib_module_names: that
# describes the backend host and can differ from the interpreter in Docker.
# In particular, distutils, imp, and asynchat are not available in Python 3.12.
_PYTHON_312_STDLIB_MODULES = frozenset(
    {
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asyncio",
        "atexit",
        "audioop",
        "base64",
        "bdb",
        "binascii",
        "bisect",
        "bz2",
        "calendar",
        "cgi",
        "cgitb",
        "chunk",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "doctest",
        "email",
        "encodings",
        "enum",
        "errno",
        "faulthandler",
        "filecmp",
        "fileinput",
        "fnmatch",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "imaplib",
        "imghdr",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "multiprocessing",
        "netrc",
        "nntplib",
        "numbers",
        "operator",
        "optparse",
        "os",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "socket",
        "socketserver",
        "sqlite3",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "zoneinfo",
    }
)
_PYTHON_IMAGE_VERSION_RE = re.compile(
    r"(?:^|/)(?:python):(?P<major>\d+)\.(?P<minor>\d+)(?:[-.@:]|$)",
    re.IGNORECASE,
)


def _python_stdlib_modules_for_image(image: str) -> frozenset[str] | None:
    """Return a known stdlib set for the configured image, or fail closed."""
    match = _PYTHON_IMAGE_VERSION_RE.search(str(image or "").strip())
    if match is None:
        return None
    version = (int(match.group("major")), int(match.group("minor")))
    if version == (3, 12):
        return _PYTHON_312_STDLIB_MODULES
    # An image with an unsupported or untagged Python version is not safe to
    # classify from the host's module inventory. Treat imports as host-specific
    # until that image gets an explicit compatibility table or probe.
    return None


_PYTHON_PROCESS_MODULES = frozenset({"asyncio", "multiprocessing", "pty", "subprocess"})
_PYTHON_PROCESS_CALLS_BY_MODULE = {
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "multiprocessing": frozenset({"Pool", "Process", "fork", "forkserver", "spawn"}),
    "pty": frozenset({"spawn"}),
    "subprocess": frozenset(
        {
            "call",
            "check_call",
            "check_output",
            "Popen",
            "run",
        }
    ),
    "os": frozenset(
        {
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "fork",
            "startfile",
            "system",
        }
    ),
}
_PYTHON_PROCESS_NAMES = frozenset({"Popen", "Pool", "Process"})
_PYTHON_DYNAMIC_IMPORT_CALLS = frozenset({"__import__", "import_module"})

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
        self._python_stdlib_modules = _python_stdlib_modules_for_image(settings.docker.image)

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

        backend_mode = (
            self.settings.mode
            if self.settings.mode in {"docker", "enforce"}
            else request.requested_backend or self.settings.mode
        )
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
            if backend_mode in {"docker", "enforce"}:
                if shell_fallback:
                    return self._blocked(
                        profile,
                        network,
                        write_strategy,
                        (
                            f"Docker mode requires bash, but the requested command resolves to shell "
                            f"'{effective_shell}'. Use shell='bash' with a Docker-compatible command, "
                            "or switch to auto or host for approval-required host execution."
                        ),
                        "unsupported_shell_for_backend",
                        requested_shell=request.shell,
                        effective_shell=effective_shell,
                    )
                return self._blocked(
                    profile,
                    network,
                    write_strategy,
                    self._host_fallback_reason(request.command, runner="blocked"),
                    "docker_command_not_compatible",
                    requested_shell=request.shell,
                    effective_shell=effective_shell,
                )
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
            if host_tool_fallback:
                requested_host_shell = effective_shell
                effective_shell = self._host_fallback_shell(effective_shell)
            else:
                requested_host_shell = effective_shell
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
                    + self._host_fallback_shell_note(request, requested_host_shell, effective_shell)
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
                    + self._host_fallback_shell_note(request, requested_host_shell, effective_shell)
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
    def _host_fallback_shell(effective_shell: str) -> str:
        """Resolve the shell that the advisory host runner can actually use."""
        if str(effective_shell or "").lower() != "bash":
            return effective_shell
        if os.name == "nt" and shutil.which("bash") is None:
            return "powershell"
        return "bash"

    def _requires_host_runner(self, command: str) -> bool:
        text = str(command or "")
        if not text.strip():
            return False
        # Command substitutions can invoke an arbitrary executable without
        # appearing at a shell segment boundary. Keep those on the host;
        # explicit bash requests still go through the image allowlist check.
        if "$(" in text or "`" in text:
            return True
        for segment in SandboxPolicy._split_shell_segments(text):
            executable = SandboxPolicy._segment_executable(segment)
            if not executable:
                return True
            if executable in _PYTHON_EXECUTABLES and self._python_host_requirement(segment):
                return True
            if executable in _PIP_EXECUTABLES and SandboxPolicy._pip_state_changing(segment):
                return True
            if executable in {"bash", "sh"}:
                nested = re.search(r"(?:^|\s)-{1,2}(?:l?c|command)\s+(.+)$", segment, re.IGNORECASE)
                if nested:
                    inner = nested.group(1).strip()
                    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in {'"', "'"}:
                        inner = inner[1:-1]
                    if self._requires_host_runner(inner):
                        return True
            if executable not in _CONTAINER_SAFE_EXECUTABLES:
                return True
            if executable == "find" and re.search(
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

    def _host_tool_name(self, command: str) -> str:
        for segment in SandboxPolicy._split_shell_segments(str(command or "")):
            executable = SandboxPolicy._segment_executable(segment)
            if not executable:
                continue
            if executable in _PYTHON_EXECUTABLES and self._python_host_requirement(segment):
                return executable
            if executable in _PIP_EXECUTABLES and SandboxPolicy._pip_state_changing(segment):
                return executable
            if executable not in _CONTAINER_SAFE_EXECUTABLES:
                return executable
        if re.search(r"\b(?:python|python3)\s+-m\s+pytest\b", command, re.IGNORECASE):
            return "pytest"
        if re.search(r"\b(?:python|python3)\s+-m\s+pip\b", command, re.IGNORECASE):
            return "pip"
        return "host tool"

    def _host_fallback_reason(self, command: str, *, runner: str) -> str:
        blocked = runner == "blocked"
        suffix = "will run directly on the host" if runner == "direct" else "will use the advisory host runner"
        for segment in SandboxPolicy._split_shell_segments(str(command or "")):
            executable = SandboxPolicy._segment_executable(segment)
            requirement = (
                self._python_host_requirement(segment)
                if executable in _PYTHON_EXECUTABLES
                else None
            )
            if requirement == "script":
                if blocked:
                    return "Python script execution needs the host interpreter and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"Python script execution uses the host interpreter; this command {suffix} after explicit approval"
            if requirement == "module":
                if blocked:
                    return "Python module execution needs the host interpreter and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"Python module execution uses the host interpreter; this command {suffix} after explicit approval"
            if requirement == "dependency":
                if blocked:
                    return "Python code uses host dependencies (project or non-standard-library imports) and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"Python code uses host dependencies; this command {suffix} after explicit approval"
            if requirement == "project_path":
                if blocked:
                    return "Python code uses a project-specific import path and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"Python code uses a project-specific import path; this command {suffix} after explicit approval"
            if requirement == "process":
                if blocked:
                    return "Python code can spawn processes and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"Python code can spawn processes; this command {suffix} after explicit approval"
            if requirement == "unverifiable":
                if blocked:
                    return "Python code could not be safely verified for Docker execution. Use auto or host mode for approval-required host execution."
                return f"Python code could not be safely verified for Docker execution; this command {suffix} after explicit approval"
            if executable in _PIP_EXECUTABLES and self._pip_state_changing(segment):
                if blocked:
                    return "This pip subcommand changes the environment or writes package artifacts and is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
                return f"This pip subcommand changes the environment or writes package artifacts; this command {suffix} after explicit approval"
        if re.search(
            r"\b(?:pip|pip3)\s+(?:download|install|lock|uninstall|wheel)\b|\b(?:pip|pip3)\s+(?:cache\s+(?:purge|remove)|config\s+(?:rename|set|unset))\b|\b(?:uv\s+pip|poetry)\s+(?:download|install|lock|uninstall|wheel)\b",
            str(command or ""),
            re.IGNORECASE,
        ):
            if blocked:
                return "This package-management command is not compatible with the pinned Docker image. Use auto or host mode for approval-required host execution."
            return f"This package-management command is host-specific; this command {suffix} after explicit approval"
        tool = self._host_tool_name(command)
        if blocked:
            return (
                f"The command uses '{tool}', which is not in the pinned Docker image's safe command allowlist. "
                "Use an allowlisted command, or switch to auto or host for approval-required host execution."
            )
        return (
            f"The command uses '{tool}', which is not in the pinned Docker image's safe command allowlist; "
            f"this command {suffix} after explicit approval"
        )

    @staticmethod
    def _host_fallback_shell_note(
        request: SandboxRunRequest,
        requested_shell: str,
        effective_shell: str,
    ) -> str:
        if str(requested_shell or "").lower() == "bash" and effective_shell != "bash":
            if str(request.shell or "auto").lower() == "bash":
                return (
                    f" Requested shell 'bash' was overridden with '{effective_shell}' for host execution."
                )
            return (
                f" Docker-compatible shell 'bash' was unavailable; host execution uses '{effective_shell}'."
            )
        return ""

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
        text = SandboxPolicy._strip_leading_assignments(segment)
        match = re.match(r"[!\s]*([A-Za-z0-9_./\\-]+)", text)
        if match is None:
            return ""
        return match.group(1).replace("\\", "/").rsplit("/", 1)[-1].lower()

    @staticmethod
    def _strip_leading_assignments(segment: str) -> str:
        text = str(segment or "").strip()
        while True:
            assignment = re.match(r"^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S+)\s+", text)
            if assignment is None:
                break
            text = text[assignment.end():].lstrip()
        return text

    @staticmethod
    def _shell_words(text: str) -> list[str]:
        return re.findall(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+', str(text or ""))

    @staticmethod
    def _unquote_shell_word(token: str) -> str:
        value = str(token or "")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
            if token[0] == '"':
                value = re.sub(r'\\(["\\])', r"\1", value)
        return value

    @staticmethod
    def _python_command_tokens(segment: str) -> list[str]:
        text = SandboxPolicy._strip_leading_assignments(segment)
        executable_match = re.match(r"[!\s]*[A-Za-z0-9_./\\-]+", text)
        if executable_match is None:
            return []
        return SandboxPolicy._shell_words(text[executable_match.end():].strip())

    @staticmethod
    def _python_code_argument(segment: str) -> str | None:
        tokens = SandboxPolicy._python_command_tokens(segment)
        for index, token in enumerate(tokens):
            normalized = SandboxPolicy._unquote_shell_word(token).lower()
            if normalized in {"-c", "--command"}:
                if index + 1 >= len(tokens):
                    return None
                return SandboxPolicy._unquote_shell_word(tokens[index + 1])
            if normalized.startswith("-c=") or normalized.startswith("--command="):
                return SandboxPolicy._unquote_shell_word(token).split("=", 1)[1]
        return "" if any(SandboxPolicy._unquote_shell_word(token).lower() == "-" for token in tokens) else None

    @staticmethod
    def _python_has_code_option(segment: str) -> bool:
        return any(
            (
                SandboxPolicy._unquote_shell_word(token).lower() in {"-c", "--command"}
                or SandboxPolicy._unquote_shell_word(token).lower().startswith(("-c=", "--command="))
            )
            for token in SandboxPolicy._python_command_tokens(segment)
        )

    @staticmethod
    def _python_uses_project_path(segment: str) -> bool:
        return bool(re.search(r"(?:^|\s)(?:PYTHONPATH|PYTHONHOME)=", str(segment or ""), re.IGNORECASE))

    def _python_host_requirement(self, segment: str) -> str | None:
        executable = SandboxPolicy._segment_executable(segment)
        if executable not in _PYTHON_EXECUTABLES:
            return None
        if SandboxPolicy._python_script_argument(segment):
            tokens = SandboxPolicy._python_command_tokens(segment)
            if any(SandboxPolicy._unquote_shell_word(token).lower() in {"-m", "--module"} for token in tokens):
                return "module"
            return "script"
        if SandboxPolicy._python_uses_project_path(segment):
            return "project_path"
        code = SandboxPolicy._python_code_argument(segment)
        if code is None:
            return "unverifiable" if SandboxPolicy._python_has_code_option(segment) else None
        if not code:
            return "unverifiable"
        try:
            tree = ast.parse(code, mode="exec")
        except SyntaxError:
            return "unverifiable"

        dependency = False
        process = False
        imported_module_aliases: dict[str, str] = {}
        imported_process_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if self._python_stdlib_modules is None:
                    return "unverifiable"
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    imported_module_aliases[alias.asname or root] = root
                    if root not in self._python_stdlib_modules:
                        dependency = True
            elif isinstance(node, ast.ImportFrom):
                if self._python_stdlib_modules is None:
                    return "unverifiable"
                root = (node.module or "").split(".", 1)[0]
                if node.level or not root:
                    dependency = True
                elif root not in self._python_stdlib_modules:
                    dependency = True
                elif root in _PYTHON_PROCESS_MODULES or root == "os":
                    for alias in node.names:
                        if alias.name in _PYTHON_PROCESS_CALLS_BY_MODULE.get(root, ()):
                            imported_process_names.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    if function.id in imported_process_names or function.id in _PYTHON_PROCESS_NAMES:
                        process = True
                    elif function.id in _PYTHON_DYNAMIC_IMPORT_CALLS:
                        dependency = True
                elif isinstance(function, ast.Attribute):
                    root_node = function
                    while isinstance(root_node, ast.Attribute):
                        root_node = root_node.value
                    root = root_node.id if isinstance(root_node, ast.Name) else ""
                    module = imported_module_aliases.get(root, root)
                    if function.attr in _PYTHON_PROCESS_CALLS_BY_MODULE.get(module, ()):
                        process = True
                    if function.attr in _PYTHON_DYNAMIC_IMPORT_CALLS:
                        dependency = True
        if process:
            return "process"
        if dependency:
            return "dependency"
        return None

    @staticmethod
    def _python_script_argument(segment: str) -> bool:
        executable = SandboxPolicy._segment_executable(segment)
        if executable not in _PYTHON_EXECUTABLES:
            return False
        tokens = SandboxPolicy._python_command_tokens(segment)
        for token in tokens:
            normalized = SandboxPolicy._unquote_shell_word(token)
            if normalized in {"-c", "--command", "-"} or normalized.startswith(("-c=", "--command=")):
                return False
            if normalized in {"-m", "--module"}:
                return True
            if normalized.startswith("-"):
                continue
            return True
        return False

    @staticmethod
    def _pip_state_changing(segment: str) -> bool:
        executable = SandboxPolicy._segment_executable(segment)
        if executable not in _PIP_EXECUTABLES:
            return False
        tokens = SandboxPolicy._shell_words(SandboxPolicy._strip_leading_assignments(segment))
        if not tokens:
            return False
        subcommand = ""
        subcommand_index = -1
        index = 1
        while index < len(tokens):
            token = tokens[index]
            normalized = SandboxPolicy._unquote_shell_word(token).lower()
            if normalized == "--":
                if index + 1 < len(tokens):
                    subcommand = SandboxPolicy._unquote_shell_word(tokens[index + 1]).lower()
                    subcommand_index = index + 1
                break
            if normalized.startswith("-"):
                option = normalized.split("=", 1)[0]
                index += 2 if "=" not in normalized and option in _PIP_OPTIONS_WITH_VALUES else 1
                continue
            subcommand = normalized
            subcommand_index = index
            break
        if subcommand in _PIP_STATE_CHANGING_SUBCOMMANDS:
            return True
        if subcommand in _PIP_STATE_CHANGING_ACTIONS and subcommand_index >= 0:
            for token in tokens[subcommand_index + 1:]:
                normalized = SandboxPolicy._unquote_shell_word(token).lower()
                if normalized.startswith("-"):
                    continue
                return normalized in _PIP_STATE_CHANGING_ACTIONS[subcommand]
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
