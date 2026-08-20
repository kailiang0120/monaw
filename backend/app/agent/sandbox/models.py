from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SandboxBackend = Literal["none", "local_direct", "docker", "local_restricted"]
SandboxMode = Literal["off", "disabled", "auto", "enforce", "host", "docker", "local_restricted"]
SandboxProfile = Literal["standard", "untrusted", "host_required", "blocked"]
SandboxTrustClass = Literal["trusted", "untrusted", "blocked"]
SandboxIsolationStrength = Literal["none", "strong"]
SandboxSecurityLabel = Literal["none", "compat", "advisory", "medium", "strong"]
SandboxNetworkMode = Literal["deny", "allow"]
SandboxNetworkEnforcement = Literal["none", "advisory", "enforced"]
SandboxWriteStrategy = Literal["discard", "copy_out", "direct_rw"]


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
    enabled: bool = True
    trust_class: str = "trusted"
    required_isolation: str = "none"
    network_enforcement: str = "none"
    filesystem_policy: str = "none"
    write_strategy: str = "discard"
    reason: str = ""
    reason_code: str = "allowed"


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
    cancel_event: Any | None = Field(default=None, exclude=True, repr=False)


class SandboxExecutionResult(BaseModel):
    status: Literal["ok", "error", "blocked"]
    exit_code: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
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

    def for_backend(self, backend: SandboxBackend) -> SandboxBackendCapability | None:
        if backend in {"none", "local_direct"}:
            return None
        return getattr(self, backend)


class SandboxDecision(BaseModel):
    allowed: bool
    required: bool
    trust_class: SandboxTrustClass = "trusted"
    required_isolation: SandboxIsolationStrength = "none"
    profile: SandboxProfile
    backend: SandboxBackend
    mode: SandboxMode
    security_label: SandboxSecurityLabel
    network: SandboxNetworkMode
    network_enforcement: SandboxNetworkEnforcement
    write_strategy: SandboxWriteStrategy
    filesystem_policy: str = "none"
    explicit_approval_required: bool = False
    reason: str = ""
    reason_code: str = "allowed"


class SandboxStatus(BaseModel):
    enabled: bool
    mode: SandboxMode
    default_profile: SandboxProfile
    default_network: Literal["deny", "allow_with_approval", "allow"]
    default_write_strategy: SandboxWriteStrategy
    selected_backend: str = ""
    isolation: str = "none"
    reason_code: str = ""
    backends: dict[str, dict]
