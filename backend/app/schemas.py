import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.identity import DEFAULT_AGENT_NAME
from app.agent.llm_constants import DEFAULT_VISION_FALLBACK_MODEL

# M3: Allowed conversation_id pattern — alphanumeric, hyphens, underscores, 1-64 chars.
# Rejects path traversal characters and empty strings.
_CONV_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    attachments: list["AttachmentRef"] = Field(default_factory=list)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _CONV_ID_RE.match(v):
            raise ValueError(
                "conversation_id must be 1-64 alphanumeric characters, hyphens, or underscores"
            )
        return v


class AttachmentRef(BaseModel):
    id: str
    name: str
    path: str = ""
    mime_type: str = ""
    size: int = Field(0, ge=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    conversation_id: str = ""
    expires_at: int | None = None


class AttachmentUploadRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    data_base64: str = Field(..., min_length=1)
    mime_type: str = ""
    conversation_id: Optional[str] = None

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _CONV_ID_RE.match(v):
            raise ValueError(
                "conversation_id must be 1-64 alphanumeric characters, hyphens, or underscores"
            )
        return v


class AttachmentUploadResponse(AttachmentRef):
    pass


class SpeechToTextTranscriptionRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    data_base64: str = Field(..., min_length=1)
    mime_type: str = ""


class SpeechToTextTranscriptionResponse(BaseModel):
    text: str


class ConversationCreate(BaseModel):
    title: str = "New Conversation"


class ConversationRename(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class ConversationOut(BaseModel):
    id: str
    title: str
    created_at: str


class ContextUsageBreakdownItemPayload(BaseModel):
    key: str
    label: str
    tokens: int
    percentage: float
    kind: str = Field("used", pattern="^(used|reserved|free)$")
    detail: str = ""
    count: int = 0


class ContextUsagePayload(BaseModel):
    used: int
    limit: int
    compaction_at: int
    percentage: float
    free_tokens: int = 0
    compaction_buffer_tokens: int = 0
    tool_call_count: int = 0
    loaded_tool_count: int = 0
    estimator: str = ""
    breakdown: list[ContextUsageBreakdownItemPayload] = Field(default_factory=list)
    notes: str = ""


class OkResponse(BaseModel):
    ok: bool = True


class ChatJobCreateResponse(BaseModel):
    job_id: str
    conversation_id: str


class ChatJobOut(BaseModel):
    job_id: str
    conversation_id: str
    status: str
    started_at: float
    workflow_engine: str = ""
    active_graph_node: str = ""
    checkpoint_status: str = ""
    last_resume_reason: str = ""


VALID_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


class ConfirmationSettingsPayload(BaseModel):
    mutate: bool = True
    delete: bool = True
    launch_app: bool = True
    click: bool = True
    type: bool = True


class PathRulePayload(BaseModel):
    path: str
    read: bool = True
    write: bool = True
    delete: bool = True
    launch: bool = False
    require_confirmation: bool = False
    enabled: bool = True


class AppRulePayload(BaseModel):
    alias: str
    display_name: str = ""
    exe_paths: list[str] = Field(default_factory=list)
    launch_allowed: bool = True
    uia_allowed: bool = True
    screen_fallback_allowed: bool = False
    require_confirmation: bool = False
    enabled: bool = True


class PermissionProfilePayload(BaseModel):
    confirmations: ConfirmationSettingsPayload = Field(default_factory=ConfirmationSettingsPayload)
    blocked_roots: list[str] = Field(default_factory=list)
    path_rules: list[PathRulePayload] = Field(default_factory=list)
    app_rules: list[AppRulePayload] = Field(default_factory=list)
    allow_delete: bool = False
    dangerous_actions_require_confirm: bool = True
    allow_screen_fallback: bool = False


class LLMSettingsPayload(BaseModel):
    provider: str = Field("openai", pattern="^(openai|deepseek|gemini)$")
    model_name: str = "gpt-5.4"
    reasoning_effort: str = Field("medium", pattern="^(none|minimal|low|medium|high|xhigh|max)$")
    vision_fallback_enabled: bool = True
    vision_fallback_model: str = DEFAULT_VISION_FALLBACK_MODEL
    max_iterations_per_turn: int = Field(40, ge=1, le=500)
    max_turn_seconds: int = Field(1800, ge=30, le=14400)
    max_llm_call_seconds: int = Field(300, ge=30, le=1800)


class SpeechToTextSettingsPayload(BaseModel):
    engine: str = Field("local", pattern="^(local|cloud)$")
    local_model: str = "base"
    cloud_provider: str = Field("gemini", pattern="^gemini$")
    cloud_model: str = "gemini-2.5-flash"


class MCPServerConfigPayload(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    enabled: bool = True
    transport: str = Field("stdio", pattern="^(stdio|streamable_http)$")
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    startup_timeout_ms: int = 8000
    call_timeout_ms: int = 30000
    reconnect_on_unhealthy: bool = True
    allow_list: list[str] = Field(default_factory=list)
    description: str = ""


class MCPSettingsPayload(BaseModel):
    enabled: bool = True
    servers: list[MCPServerConfigPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def server_names_must_be_unique(self) -> "MCPSettingsPayload":
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        return self


class BrowserUseSettingsPayload(BaseModel):
    mode: str = Field("auto", pattern="^(auto|managed|system)$")
    enable_system_fallback: bool = True
    headless: bool = False
    keep_alive: bool = True
    dom_inspection_engine: str = Field("auto", pattern="^(auto|enhanced|legacy)$")
    paint_order_filtering: bool = True
    cross_origin_iframes: bool = False
    max_iframes: int = Field(5, ge=0, le=20)
    max_iframe_depth: int = Field(2, ge=0, le=5)
    system_connection_strategy: str = Field("auto", pattern="^(auto|attach|launch)$")
    system_cdp_url: str = "http://127.0.0.1:9222"
    managed_profile_dir: str = ""
    downloads_dir: str = ""
    screenshots_dir: str = ""
    traces_dir: str = ""
    system_profile_directory: str = ""
    allowed_domains: list[str] = Field(default_factory=list)


class MemorySettingsPayload(BaseModel):
    enabled: bool = True
    auto_learn: bool = True
    curate_on_session_close: bool = True
    write_policy: str = Field("auto_with_review", pattern="^(off|manual|auto_with_review|auto_reviewed)$")
    retrieval_limit: int = Field(6, ge=1, le=20)
    max_injected_chars: int = Field(2500, ge=500, le=10000)
    min_confidence: float = Field(0.75, ge=0.0, le=1.0)
    min_relevance_score: float = Field(0.15, ge=0.0, le=1.0)
    maintenance_cooldown_hours: int = Field(24, ge=0, le=168)


class ToolSettingsPayload(BaseModel):
    skills: dict[str, bool] = Field(default_factory=dict)


class SkillDescriptorPayload(BaseModel):
    slug: str
    name: str
    description: str
    version: str
    enabled_by_default: bool
    enabled: bool
    available: bool
    always: bool = False
    unavailable_reason: str = ""
    tier: str = Field("recommended", pattern="^(recommended|optional)$")
    recommended: bool = True


class PermissionSettingsPayload(BaseModel):
    mode: str = Field("default", pattern="^(default|full_access|custom|user_config)$")
    confirmations: ConfirmationSettingsPayload = Field(default_factory=ConfirmationSettingsPayload)
    blocked_roots: list[str] = Field(default_factory=list)
    path_rules: list[PathRulePayload] = Field(default_factory=list)
    app_rules: list[AppRulePayload] = Field(default_factory=list)
    allow_delete: bool = False
    dangerous_actions_require_confirm: bool = True
    allow_screen_fallback: bool = False
    custom_profile: PermissionProfilePayload = Field(default_factory=PermissionProfilePayload)


class SandboxResourceLimitsPayload(BaseModel):
    timeout_seconds: int = Field(120, ge=1, le=14400)
    memory_mb: int = Field(1024, ge=128, le=32768)
    cpus: float = Field(1.0, ge=0.1, le=16.0)
    pids: int = Field(128, ge=16, le=4096)
    max_output_bytes: int = Field(1048576, ge=4096, le=104857600)
    max_workspace_mb: int = Field(1024, ge=16, le=102400)


class SandboxNetworkSettingsPayload(BaseModel):
    default: str = Field("deny", pattern="^(deny|allow_with_approval|allow)$")
    allow_domains: list[str] = Field(default_factory=list)


class SandboxDockerSettingsPayload(BaseModel):
    enabled: bool = True
    image: str = "python:3.12-slim"
    extra_images: list[str] = Field(default_factory=list)
    pull_policy: str = Field("missing", pattern="^(never|missing|always)$")
    read_only_root: bool = True
    no_new_privileges: bool = True


class SandboxLocalRestrictedSettingsPayload(BaseModel):
    enabled: bool = True
    use_job_object: bool = True
    kill_process_tree_on_timeout: bool = True
    strip_environment: bool = True


class SandboxWslSettingsPayload(BaseModel):
    enabled: bool = False
    distro: str = ""
    note_network_isolation_is_advisory: bool = True


class SandboxSettingsPayload(BaseModel):
    enabled: bool = True
    mode: str = Field("auto", pattern="^(off|disabled|auto|enforce|host|docker|local_restricted|wsl)$")
    default_profile: str = Field("standard", pattern="^(standard|untrusted|project_write|host_required)$")
    require_strong_for_untrusted: bool = True
    default_write_strategy: str = Field("copy_out", pattern="^(discard|copy_out|direct_rw)$")
    allowed_bind_roots: list[str] = Field(default_factory=list)
    blocked_bind_roots: list[str] = Field(default_factory=list)
    preserve_artifacts_days: int = Field(14, ge=1, le=365)
    resources: SandboxResourceLimitsPayload = Field(default_factory=SandboxResourceLimitsPayload)
    network: SandboxNetworkSettingsPayload = Field(default_factory=SandboxNetworkSettingsPayload)
    docker: SandboxDockerSettingsPayload = Field(default_factory=SandboxDockerSettingsPayload)
    local_restricted: SandboxLocalRestrictedSettingsPayload = Field(
        default_factory=SandboxLocalRestrictedSettingsPayload
    )
    wsl: SandboxWslSettingsPayload = Field(default_factory=SandboxWslSettingsPayload)


class IdentitySettingsPayload(BaseModel):
    agent_name: str = Field(DEFAULT_AGENT_NAME, max_length=80)
    user_name: str = Field("", max_length=80)
    user_identity: str = Field("", max_length=1000)
    communication_style: str = Field("", max_length=1000)


class ApiKeyStatusPayload(BaseModel):
    has_openai_key: bool
    has_deepseek_key: bool = False
    has_google_key: bool
    has_tavily_key: bool = False
    has_telegram_bot_token: bool = False
    has_telegram_allowlist: bool = False


class AgentSettingsPayload(BaseModel):
    llm: LLMSettingsPayload
    speech_to_text: SpeechToTextSettingsPayload
    mcp: MCPSettingsPayload
    browser: BrowserUseSettingsPayload
    memory: MemorySettingsPayload
    tools: ToolSettingsPayload
    permissions: PermissionSettingsPayload
    sandbox: SandboxSettingsPayload
    identity: IdentitySettingsPayload
    settings_version: str = ""
    available_skills: list[SkillDescriptorPayload] = Field(default_factory=list)
    api_keys: ApiKeyStatusPayload | None = None
    telegram_allowed_user_ids: str = ""
    telegram_allowed_chat_ids: str = ""


class ModelProviderOptionPayload(BaseModel):
    id: str
    label: str
    models: list[str] = Field(default_factory=list)


class ModelOptionsPayload(BaseModel):
    providers: list[ModelProviderOptionPayload] = Field(default_factory=list)
    vision_fallback_models: list[str] = Field(default_factory=list)


class WorkspaceInstructionsPayload(BaseModel):
    path: str
    content: str


class SpeechToTextStatusPayload(BaseModel):
    provider: str
    label: str
    engine: str
    cloud_provider: str = "gemini"
    cloud_model: str = ""
    cloud_configured: bool = False
    model_id: str
    model_label: str
    model_size: str
    downloaded: bool
    download_dir: str
    dependency_available: bool
    loaded: bool


class ControllerPolicyMarkdownPayload(BaseModel):
    policy_markdown: str
    allowlist_markdown: str


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_settings_version: Optional[str] = Field(None, min_length=1, max_length=128)
    llm: Optional[LLMSettingsPayload] = None
    speech_to_text: Optional[SpeechToTextSettingsPayload] = None
    mcp: Optional[MCPSettingsPayload] = None
    browser: Optional[BrowserUseSettingsPayload] = None
    memory: Optional[MemorySettingsPayload] = None
    tools: Optional[ToolSettingsPayload] = None
    permissions: Optional[PermissionSettingsPayload] = None
    sandbox: Optional[SandboxSettingsPayload] = None
    identity: Optional[IdentitySettingsPayload] = None
    model_provider: Optional[str] = Field(None, pattern="^(openai|deepseek|gemini)$")
    openai_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    google_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_allowed_user_ids: Optional[str] = None
    telegram_allowed_chat_ids: Optional[str] = None
    model_name: Optional[str] = None
    reasoning_effort: Optional[str] = Field(None, pattern="^(none|minimal|low|medium|high|xhigh|max)$")
    controller_permission_mode: Optional[str] = Field(
        None, pattern="^(default|full_access|custom|user_config)$"
    )


class WorkspaceInstructionsUpdate(BaseModel):
    content: str = Field("", max_length=20_000)


class AppEntryCreate(BaseModel):
    alias: str
    display_name: str = ""
    exe_paths: list[str] = Field(default_factory=list)


class AppEntryOut(BaseModel):
    alias: str
    display_name: str
    exe_paths: list[str]
    added_at: str


class ControllerPolicyOut(BaseModel):
    mode: str
    permitted_roots: list[str]
    blocked_roots: list[str]
    allow_delete: bool
    dangerous_actions_require_confirm: bool
    allowlisted_apps: list[AppEntryOut]


class ControllerPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Optional[str] = Field(None, pattern="^(default|full_access|custom|user_config)$")
    permitted_roots: Optional[list[str]] = None
    blocked_roots: Optional[list[str]] = None
    allow_delete: Optional[bool] = None
    dangerous_actions_require_confirm: Optional[bool] = None


class RuntimeFileStatusPayload(BaseModel):
    path: str = ""
    exists: bool
    size_bytes: int = 0


class DiagnosticsBackendPayload(BaseModel):
    name: str
    version: str
    port: int
    runtime_dir: str = ""
    database: RuntimeFileStatusPayload


class DiagnosticsSchedulerPayload(BaseModel):
    running: bool = False
    stopping: bool = False
    inflight_tasks: int = 0
    startup_error: str = ""


class DiagnosticsTelegramPayload(BaseModel):
    configured: bool = False
    running: bool = False
    startup_error: str = ""


class DiagnosticsMcpSummaryPayload(BaseModel):
    enabled: bool
    configured_servers: int
    runtime_servers: int
    connected_servers: int
    unhealthy_servers: int


class DiagnosticsBrowserSummaryPayload(BaseModel):
    available: bool
    session_active: bool
    preferred_mode: str
    current_mode: str
    last_error: str


class SandboxBackendCapabilityPayload(BaseModel):
    backend: str
    enabled: bool
    available: bool
    security_label: str
    network_enforcement: str
    version: str = ""
    reason: str = ""


class SandboxStatusPayload(BaseModel):
    enabled: bool
    mode: str
    default_profile: str
    default_network: str
    default_write_strategy: str
    require_strong_for_untrusted: bool
    backends: dict[str, SandboxBackendCapabilityPayload]


class DiagnosticsSummaryPayload(BaseModel):
    backend: DiagnosticsBackendPayload
    scheduler: DiagnosticsSchedulerPayload
    telegram: DiagnosticsTelegramPayload
    mcp: DiagnosticsMcpSummaryPayload
    browser: DiagnosticsBrowserSummaryPayload
    sandbox: SandboxStatusPayload


class MCPServerDiagnosticsOut(BaseModel):
    name: str
    enabled: bool = False
    transport: str = "stdio"
    connected: bool = False
    state: str = ""
    tool_count: int = 0
    last_error: str | None = None
    unhealthy_reason: str | None = None
    description: str = ""
    command: str = ""
    args: list[str] = Field(default_factory=list)
    cwd: str = ""
    url: str = ""
    resolved_executable: str = ""
    startup_phase: str = ""
    pid: int | None = None
    started_at: str | None = None
    connected_at: str | None = None
    disconnected_at: str | None = None
    stderr_tail: str = ""
    last_call_started_at: str | None = None
    last_call_duration_ms: int | float | None = None
    failed_call_count: int = 0
    remote_tool_names: list[str] = Field(default_factory=list)
    reflected_tool_names: list[str] = Field(default_factory=list)
    login_capable: bool = False
    skill_enabled: bool = False
    skill_available: bool = False
    skill_unavailable_reason: str = ""
    feature_enabled: bool = False
    feature_available: bool = False
    feature_unavailable_reason: str = ""


# ── Approval ticket schemas ───────────────────────────────────────────────────


class ApprovalTicketOut(BaseModel):
    id: str
    conversation_id: str
    control_session_id: str = ""
    execution_source: str = "desktop"
    action_type: str
    tool_name: str
    target_path: str
    target_app: str
    risk_level: str
    reason: str
    action_description: str
    payload: dict = Field(default_factory=dict)
    payload_hash: str
    status: str
    created_at: str
    expires_at: str = ""
    superseded_by: str = ""
    resolved_at: str
    resolved_by: str
    execution_result: str


class ApprovalDecisionIn(BaseModel):
    resolved_by: Optional[str] = "user"


class PendingApprovalEvent(BaseModel):
    ticket_id: str
    action_description: str
    risk_level: str
    tool_name: str
    target_path: str
    target_app: str


# ── Message retrieval schemas ─────────────────────────────────────────────────


class ToolCallOut(BaseModel):
    id: int
    tool_name: str
    input: str = ""
    output: str = ""
    status: str = "complete"
    preview_only: bool = False
    has_full_input: bool = False
    has_full_output: bool = False


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    thinking: str = ""
    status: str = Field("complete", pattern="^(complete|paused|error)$")
    response_duration_ms: int | None = None
    tool_calls: list[ToolCallOut] = Field(default_factory=list)
    attachments: list[AttachmentRef] = Field(default_factory=list)
    created_at: str


class MessagesResponse(BaseModel):
    messages: list[MessageOut]
    has_more: bool
    next_before_id: int | None = None


# ── Access grant schemas ──────────────────────────────────────────────────────


class AccessGrantTicketOut(BaseModel):
    id: str
    conversation_id: str
    control_session_id: str = ""
    execution_source: str = "desktop"
    target_type: str
    target_identifier: str
    display_name: str
    action_context: str
    status: str
    created_at: str
    expires_at: str = ""
    payload_hash: str = ""
    superseded_by: str = ""
    resolved_at: str = ""


class AccessGrantDecisionIn(BaseModel):
    decision: str = Field(..., pattern="^(once|session|always|deny)$")


class AccessGrantEvent(BaseModel):
    ticket_id: str
    target_type: str
    target_identifier: str
    display_name: str
    action_context: str


# ── New orchestration event schemas ──────────────────────────────────────────


class ThinkingEvent(BaseModel):
    content: str


class PlanStepOut(BaseModel):
    step_id: str
    description: str
    tool_hints: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: str
    status: str = "pending"


class PlanEvent(BaseModel):
    plan_id: str
    original_message: str
    steps: list[PlanStepOut]


class StepStartEvent(BaseModel):
    step_id: str
    description: str


class StepCompleteEvent(BaseModel):
    step_id: str
    status: str  # "done" | "failed"


class ObservationEvent(BaseModel):
    step_id: str
    verified: bool
    detail: str


class RetryEvent(BaseModel):
    step_id: str
    reason: str
    attempt: int


class BrowserUseProfileOut(BaseModel):
    name: str
    directory: str


class BrowserUsePageOut(BaseModel):
    target_id: str
    url: str
    title: str


class BrowserUseDiagnosticsOut(BaseModel):
    available: bool
    skill_enabled: bool
    skill_available: bool
    skill_unavailable_reason: str
    session_active: bool
    preferred_mode: str
    current_mode: str = ""
    current_system_connection: str = ""
    fallback_enabled: bool = True
    dom_inspection_engine: str = "auto"
    paint_order_filtering: bool = True
    cross_origin_iframes: bool = False
    max_iframes: int = 5
    max_iframe_depth: int = 2
    last_error: str = ""
    system_connection_strategy: str = "auto"
    system_cdp_url: str = ""
    managed_profile_dir: str = ""
    downloads_dir: str = ""
    screenshots_dir: str = ""
    traces_dir: str = ""
    system_profile_directory: str = ""
    chrome_executable: str = ""
    available_system_profiles: list[BrowserUseProfileOut] = Field(default_factory=list)
    current_page: BrowserUsePageOut | None = None
    tab_count: int = 0


class ObservabilityUsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    image_tokens: int = 0
    total_tokens: int = 0
    source: str = ""


class ObservabilityTrendPointOut(BaseModel):
    day: str
    runs: int = 0
    tokens: int = 0
    average_duration_ms: float = 0


class ObservabilityReasonCountOut(BaseModel):
    reason: str
    count: int


class ObservabilityToolCountOut(BaseModel):
    tool_name: str
    count: int


class ObservabilityModelUsageOut(BaseModel):
    model: str
    provider: str
    runs: int = 0
    tokens: int = 0


class ObservabilitySummaryOut(BaseModel):
    total_runs: int = 0
    successful_runs: int = 0
    success_rate: float = 0
    failed_runs: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0
    average_duration_ms: float = 0
    tool_error_count: int = 0
    storage_path: str = ""
    token_usage_over_time: list[ObservabilityTrendPointOut] = Field(default_factory=list)
    duration_trend: list[ObservabilityTrendPointOut] = Field(default_factory=list)
    top_error_reasons: list[ObservabilityReasonCountOut] = Field(default_factory=list)
    top_failing_tools: list[ObservabilityToolCountOut] = Field(default_factory=list)
    model_usage: list[ObservabilityModelUsageOut] = Field(default_factory=list)
    storage_metrics: dict[str, Any] = Field(default_factory=dict)
    runtime_metrics: dict[str, Any] = Field(default_factory=dict)
    field_classification: dict[str, Any] = Field(default_factory=dict)


class ObservabilityRunOut(BaseModel):
    run_id: str
    conversation_id: str = ""
    message_id: str = ""
    source: str = ""
    model: str = ""
    provider: str = ""
    status: str = ""
    failure_reason: str = ""
    failure_pattern: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    user_message: str = ""
    final_output: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    image_tokens: int = 0
    total_tokens: int = 0
    usage_source: str = ""
    estimated_cost_usd: float = 0
    cost_source: str = ""
    tool_count: int = 0
    tool_error_count: int = 0
    event_count: int = 0
    metadata_json: str = ""


class ObservabilityEventOut(BaseModel):
    event_id: str
    run_id: str
    conversation_id: str = ""
    message_id: str = ""
    event_type: str = ""
    level: str = ""
    status: str = ""
    source: str = ""
    model: str = ""
    provider: str = ""
    tool_name: str = ""
    error_code: str = ""
    error_message: str = ""
    duration_ms: int | float = 0
    input: Any = None
    output: Any = None
    tokens: ObservabilityUsageOut | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ObservabilityErrorOut(BaseModel):
    error_id: str
    run_id: str = ""
    conversation_id: str = ""
    message_id: str = ""
    level: str = ""
    logger_name: str = ""
    module: str = ""
    error_type: str = ""
    message: str = ""
    traceback: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ObservabilityReplayOut(BaseModel):
    replay_id: str
    source_run_id: str
    replay_run_id: str = ""
    conversation_id: str = ""
    status_change: str = ""
    duration_delta_ms: int = 0
    token_delta: int = 0
    tool_sequence_diff: str = ""
    failure_reason_diff: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class ObservabilityRunDetailOut(ObservabilityRunOut):
    events: list[ObservabilityEventOut] = Field(default_factory=list)
    errors: list[ObservabilityErrorOut] = Field(default_factory=list)
    replays: list[ObservabilityReplayOut] = Field(default_factory=list)
    tool_sequence: list[str] = Field(default_factory=list)


class ObservabilitySupportModeOut(BaseModel):
    enabled: bool
    expires_at_epoch: float
    remaining_seconds: int


class ObservabilityBackendLogOut(BaseModel):
    path: str
    exists: bool
    size_bytes: int = 0
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: str = ""


class ObservabilityReplayResultOut(BaseModel):
    source_run_id: str
    replay_run_id: str
    conversation_id: str
    status: str
    summary: str
    errors: list[str] = Field(default_factory=list)
    comparison: ObservabilityReplayOut


class ObservabilityDebugBundleOut(BaseModel):
    filename: str
    run_id: str
    size_bytes: int = 0
    encrypted: bool = False


class DataDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool = False


class DataDeletionResult(BaseModel):
    ok: bool = True
    deleted: dict[str, int] = Field(default_factory=dict)
    deleted_at: str = ""


# ── Long-term memory schemas ────────────────────────────────────────────────


class MemoryOut(BaseModel):
    id: str
    content: str
    category: str
    status: str
    review_state: str
    confidence: float
    importance: int = 5
    kind: str = "fact"
    source: str = ""
    source_conversation_id: str = ""
    source_message_id: int | None = None
    created_at: str
    updated_at: str
    last_used_at: str = ""
    use_count: int = 0


class MemoryScoreBreakdown(BaseModel):
    exact: float = 0.0
    token: float = 0.0
    category: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    use: float = 0.0


class MemorySearchOut(MemoryOut):
    score: float = 0.0
    score_breakdown: MemoryScoreBreakdown = Field(default_factory=MemoryScoreBreakdown)


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)
    category: str = Field("fact", pattern="^(preference|behavior|fact|workflow|project|reflection)$")
    review_state: str = Field("reviewed", pattern="^(new|reviewed)$")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    importance: int = Field(5, ge=1, le=10)
    kind: str = Field("fact", pattern="^(fact|reflection)$")


class MemoryUpdate(BaseModel):
    content: Optional[str] = Field(None, min_length=1, max_length=1000)
    category: Optional[str] = Field(None, pattern="^(preference|behavior|fact|workflow|project|reflection)$")
    status: Optional[str] = Field(None, pattern="^(active|archived)$")
    review_state: Optional[str] = Field(None, pattern="^(new|reviewed)$")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    importance: Optional[int] = Field(None, ge=1, le=10)
    kind: Optional[str] = Field(None, pattern="^(fact|reflection)$")


class MemoryStatsOut(BaseModel):
    total: int
    active: int
    archived: int
    new: int
    reviewed: int
    fact: int = 0
    reflection: int = 0
    candidates: int = 0
    unresolved_candidates: int = 0
    short_term: int = 0
    personalities: int = 0
    curated_sessions: int = 0
    audit_events: int = 0
    archived_messages: int = 0
    episodes: int = 0
    active_checkpoints: int = 0
    profile_fields: int = 0
    memory_root: str = ""
    categories: dict[str, int] = Field(default_factory=dict)


class MemoryFileSectionOut(MemoryOut):
    title: str
    body: str


class MemoryFileOut(BaseModel):
    category: str
    raw_markdown: str
    sections: list[MemoryFileSectionOut] = Field(default_factory=list)
    updated_at: str = ""


class MemoryFileUpdate(BaseModel):
    raw_markdown: str = Field(..., min_length=1)


class MemorySectionUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=120)
    body: Optional[str] = Field(None, min_length=1, max_length=10000)
    importance: Optional[int] = Field(None, ge=1, le=10)
    review_state: Optional[str] = Field(None, pattern="^(new|reviewed)$")


class MemoryAuditOut(BaseModel):
    id: str
    memory_id: str | None = None
    action: str
    reason: str = ""
    source_conversation_id: str | None = ""
    candidate_content: str | None = ""
    created_at: str


class MemorySessionCloseIn(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=64)


class MemorySessionCloseOut(BaseModel):
    conversation_id: str
    reconciliation: dict[str, int]
    archived_messages: int
    maintenance: dict[str, int] = Field(default_factory=dict)
    hot_messages_before: int
    hot_messages_after: int
    reflection_id: str | None = None


class MemoryProfileFieldOut(BaseModel):
    field: str
    value: str
    privacy_level: str = "normal"
    confidence: float = 1.0
    review_state: str = "new"
    source_conversation_id: str = ""
    source_message_id: int | None = None
    updated_at: str


class MemoryProfileFieldUpdate(BaseModel):
    value: str = Field(..., min_length=1, max_length=2000)
    privacy_level: str = Field("normal", pattern="^(normal|private|sensitive)$")
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    review_state: str = Field("reviewed", pattern="^(new|reviewed)$")
    source_conversation_id: str = ""
    source_message_id: int | None = None


class MemoryCandidateOut(BaseModel):
    id: str
    kind: str = "fact"
    content: str
    category: str
    confidence: float = 0.8
    importance: int = 5
    status: str = "new"
    reason: str = ""
    source_conversation_id: str = ""
    source_message_id: int | None = None
    created_at: str
    updated_at: str


class MemoryCandidateUpdate(BaseModel):
    status: str = Field(..., pattern="^(new|approved|rejected)$")
    approve: bool = False


class MemoryCheckpointOut(BaseModel):
    id: str
    scope: str = "conversation"
    status: str = "active"
    conversation_id: str = ""
    project: str = ""
    app_name: str = ""
    goal: str = ""
    last_known_state: str = ""
    next_action: str = ""
    blocker: str = ""
    browser_url: str = ""
    browser_title: str = ""
    workspace_path: str = ""
    files_touched: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)
    expires_at: str = ""
    source_refs: list[dict] = Field(default_factory=list)
    created_at: str
    updated_at: str


class MemoryEpisodeOut(BaseModel):
    id: str
    conversation_id: str
    channel: str = "desktop"
    project: str = ""
    task_type: str = ""
    summary: str
    decisions: list = Field(default_factory=list)
    artifacts: list = Field(default_factory=list)
    errors: list = Field(default_factory=list)
    fixes: list = Field(default_factory=list)
    open_questions: list = Field(default_factory=list)
    follow_ups: list = Field(default_factory=list)
    source_message_start_id: int | None = None
    source_message_end_id: int | None = None
    tool_call_ids: list[int] = Field(default_factory=list)
    created_at: str
    updated_at: str
