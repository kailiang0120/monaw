from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SandboxBackend = Literal["none", "local_direct", "docker", "local_restricted", "wsl"]
SandboxMode = Literal["off", "disabled", "auto", "enforce", "docker", "local_restricted", "wsl"]
SandboxProfile = Literal["standard", "untrusted", "project_write", "host_required", "blocked"]
SandboxSecurityLabel = Literal["none", "compat", "advisory", "medium", "strong"]
SandboxNetworkMode = Literal["deny", "allow"]
SandboxNetworkEnforcement = Literal["none", "advisory", "enforced"]
SandboxWriteStrategy = Literal["discard", "copy_out", "direct_rw"]


class SandboxMount(BaseModel):
    source: str
    target: str
    read_only: bool = True


class SandboxLimits(BaseModel):
    timeout_seconds: int = 120
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 128
    max_output_bytes: int = 1048576
    max_workspace_mb: int = 1024


class SandboxRunRequest(BaseModel):
    command: str
    shell: Literal["powershell", "pwsh", "cmd", "bash"] = "powershell"
    workdir: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 60
    profile: SandboxProfile | None = None
    requested_backend: SandboxMode | None = None
    network: SandboxNetworkMode | None = None
    write_strategy: SandboxWriteStrategy | None = None
    mounts: list[SandboxMount] = Field(default_factory=list)
    elevated: bool = False
    approval_id: str = ""
    conversation_id: str = ""
    tool_call_id: str = ""


class SandboxRunMetadata(BaseModel):
    backend: str
    selected_backend: str = ""
    security_label: str = "none"
    filesystem_isolation: str = "none"
    process_isolation: str = "none"
    process_cleanup: str = ""
    network_isolation: str = "none"
    env_inheritance: str = "scrubbed"
    profile: str = "standard"
    mode: str = "auto"
    network: str = "deny"
    workdir: str = ""
    inherited_env_keys: list[str] = Field(default_factory=list)
    explicit_env_keys: list[str] = Field(default_factory=list)
    blocked_env_keys: list[str] = Field(default_factory=list)
    env_keys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    artifacts: dict = Field(default_factory=dict)


class SandboxExecutionRequest(BaseModel):
    command: str
    shell: Literal["powershell", "pwsh", "cmd", "bash"] = "powershell"
    workdir: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 60
    max_stdout: int = 8192
    max_stderr: int = 4096
    tail_lines: int = 0
    return_mode: Literal["full", "head_tail", "tail", "summary"] = "full"
    save_output_to: str = ""
    profile: SandboxProfile = "standard"
    backend: str = ""
    network: SandboxNetworkMode = "deny"
    tool_name: str = "exec"
    conversation_id: str = ""
    request_id: str = ""
    env_metadata: dict = Field(default_factory=dict)
    resources: dict = Field(default_factory=dict)
    copy_policy: dict = Field(default_factory=dict)


class SandboxExecutionResult(BaseModel):
    status: Literal["ok", "error", "blocked"]
    exit_code: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    command_id: str = ""
    stdout: str = ""
    stderr: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    shell: str = ""
    shell_command: str = ""
    workdir: str = ""
    env_keys: list[str] = Field(default_factory=list)
    sandbox: dict = Field(default_factory=dict)
    reason: str = ""
    reason_code: str = ""
    error: str = ""


class SandboxSessionStartRequest(SandboxExecutionRequest):
    pass


class SandboxSessionHandle(BaseModel):
    session_id: str
    backend: str
    pid: int | None = None
    sandbox: dict = Field(default_factory=dict)


class SandboxSessionStatus(BaseModel):
    status: Literal["running", "ok", "error"]
    command_id: str
    pid: int | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    shell: str = ""
    workdir: str = ""
    env_keys: list[str] = Field(default_factory=list)
    sandbox: dict = Field(default_factory=dict)
    reason_code: str = ""
    error: str = ""


class SandboxSessionWriteRequest(BaseModel):
    session_id: str
    text: str


class SandboxSessionStopRequest(BaseModel):
    session_id: str
    signal: Literal["terminate", "kill"] = "terminate"


class SandboxBackendCapability(BaseModel):
    backend: SandboxBackend
    enabled: bool
    available: bool
    security_label: SandboxSecurityLabel
    network_enforcement: SandboxNetworkEnforcement
    version: str = ""
    reason: str = ""


class SandboxCapabilities(BaseModel):
    docker: SandboxBackendCapability
    local_restricted: SandboxBackendCapability
    wsl: SandboxBackendCapability

    def for_backend(self, backend: SandboxBackend) -> SandboxBackendCapability | None:
        if backend in {"none", "local_direct"}:
            return None
        return getattr(self, backend)


class SandboxDecision(BaseModel):
    allowed: bool
    required: bool
    profile: SandboxProfile
    backend: SandboxBackend
    mode: SandboxMode
    security_label: SandboxSecurityLabel
    network: SandboxNetworkMode
    network_enforcement: SandboxNetworkEnforcement
    write_strategy: SandboxWriteStrategy
    reason: str = ""
    reason_code: str = "allowed"


class SandboxStatus(BaseModel):
    enabled: bool
    mode: SandboxMode
    default_profile: SandboxProfile
    default_network: Literal["deny", "allow_with_approval", "allow"]
    default_write_strategy: SandboxWriteStrategy
    require_strong_for_untrusted: bool
    backends: dict[str, dict]
