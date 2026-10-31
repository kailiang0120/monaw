export interface ConversationWorkspace {
  workspace_dir: string
}

export interface Conversation {
  id: string
  title: string
  created_at: string
  workspace_dir?: string
}

export interface OkResponse {
  ok: boolean
}

export interface ChatJobCreateResponse {
  job_id: string
  conversation_id: string
}

export interface ChatSteerResponse {
  message_id: string
  conversation_id: string
  status: string
}

export interface TurnStartedEvent {
  run_id: string
  conversation_id: string
}

export interface SteeringAppliedEvent {
  message_id: string
  message: string
  attachments?: UploadedAttachment[]
  reset_response?: boolean
}

export interface ChatJob {
  job_id: string
  conversation_id: string
  status: string
  started_at: string
  workflow_engine: string
  active_graph_node: string
  checkpoint_status: string
  last_resume_reason: string
  dropped_subscriber_events: number
}

export type ScheduleKind = 'cron' | 'interval' | 'once'
export type ScheduledTaskOverlapPolicy = 'skip' | 'queue' | 'cancel_previous'

export interface ScheduledTask {
  id: string
  title: string
  prompt: string
  scheduleKind: ScheduleKind
  cronExpr: string
  intervalSeconds: number
  runAt: string
  timezone: string
  enabled: boolean
  overlapPolicy: ScheduledTaskOverlapPolicy
  notifyTelegram: boolean
  telegramChatId: string
  reuseConversation: boolean
  lastRunAt: string
  lastRunStatus: string
  lastRunConversationId: string
  nextRunAt: string
  consecutiveFailures: number
  running: boolean
  queued?: boolean
  createdAt: string
  updatedAt: string
}

export interface ScheduledTaskRun {
  id: number
  taskId: string
  conversationId: string
  startedAt: string
  finishedAt: string
  status: 'running' | 'ok' | 'error' | 'skipped' | string
  finalText: string
  error: string
}

export interface ScheduledTaskInput {
  title: string
  prompt: string
  scheduleKind: ScheduleKind
  cronExpr?: string
  intervalSeconds?: number
  runAt?: string
  timezone?: string
  enabled?: boolean
  overlapPolicy?: ScheduledTaskOverlapPolicy
  notifyTelegram?: boolean
  telegramChatId?: string
  reuseConversation?: boolean
}

export interface SchedulePreviewRequest {
  scheduleKind: ScheduleKind
  cronExpr?: string
  intervalSeconds?: number
  runAt?: string
  timezone?: string
}

export interface TelegramChatTarget {
  id: string
  label: string
}

export interface ContextUsage {
  used: number
  limit: number
  compaction_at: number
  percentage: number
  free_tokens: number
  compaction_buffer_tokens: number
  tool_call_count: number
  loaded_tool_count: number
  estimator: string
  breakdown: {
    key: string
    label: string
    tokens: number
    percentage: number
    kind: 'used' | 'reserved' | 'free' | 'excluded'
    detail: string
    count: number
  }[]
  notes: string
}

export interface UploadedAttachment {
  id: string
  name: string
  path: string
  mime_type: string
  size: number
  width?: number
  height?: number
  conversation_id?: string
  expires_at?: number | null
}

export interface SavedMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  thinking: string
  status?: 'complete' | 'paused' | 'error'
  response_duration_ms?: number | null
  attachments?: UploadedAttachment[]
  tool_calls: {
    id: number
    tool_name: string
    input: string
    output: string
    status: string
    preview_only?: boolean
    has_full_input?: boolean
    has_full_output?: boolean
  }[]
  created_at: string
}

export interface MessagesResponse {
  messages: SavedMessage[]
  has_more: boolean
  next_before_id?: number | null
}

export interface AccessGrantTicket {
  id: string
  conversation_id: string
  target_type: string
  target_identifier: string
  display_name: string
  action_context: string
  requested_access: 'read' | 'write' | 'delete' | 'launch' | string
  status: string
  created_at: string
  resolved_at: string
}

export type AccessGrantDecision = 'once' | 'session' | 'always' | 'deny'

export interface ConfirmationSettings {
  mutate: boolean
  delete: boolean
  launch_app: boolean
  click: boolean
  type: boolean
}

export interface PathRule {
  path: string
  read: boolean
  write: boolean
  delete: boolean
  launch: boolean
  require_confirmation: boolean
  enabled: boolean
}

export interface AppRule {
  alias: string
  display_name: string
  exe_paths: string[]
  launch_allowed: boolean
  uia_allowed: boolean
  screen_fallback_allowed: boolean
  require_confirmation: boolean
  enabled: boolean
}

export interface PermissionProfile {
  confirmations: ConfirmationSettings
  blocked_roots: string[]
  path_rules: PathRule[]
  app_rules: AppRule[]
  allow_delete: boolean
  dangerous_actions_require_confirm: boolean
  allow_screen_fallback: boolean
}

export interface SkillDescriptor {
  slug: string
  name: string
  display_name: string
  summary: string
  description: string
  version: string
  enabled_by_default: boolean
  enabled: boolean
  available: boolean
  always: boolean
  unavailable_reason: string
  load_error: string
  tier: 'internal' | 'recommended' | 'optional'
  recommended: boolean
  hidden: boolean
}

export interface MCPServerConfig {
  server_id?: string
  credential_refs?: Partial<Record<'env' | 'headers', Record<string, string>>>
  legacy_secret_fields?: Partial<Record<'env' | 'headers', string[]>>
  name: string
  enabled: boolean
  transport: 'stdio' | 'streamable_http'
  command: string
  args: string[]
  env: Record<string, string>
  cwd: string
  url: string
  headers: Record<string, string>
  startup_timeout_ms: number
  call_timeout_ms: number
  reconnect_on_unhealthy: boolean
  allow_list: string[]
  trusted_tools: string[]
  tool_risk_overrides: Record<string, 'low' | 'medium' | 'high'>
  description: string
}

export interface MCPServerDiagnostics {
  name: string
  enabled: boolean
  transport: 'stdio' | 'streamable_http'
  connected: boolean
  state: string
  tool_count: number
  last_error: string | null
  unhealthy_reason: string | null
  description: string
  command: string
  args: string[]
  cwd: string
  url: string
  resolved_executable: string
  startup_phase: string
  pid: number | null
  started_at: string | null
  connected_at: string | null
  disconnected_at: string | null
  stderr_tail: string
  last_call_started_at: string | null
  last_call_duration_ms: number | null
  failed_call_count: number
  remote_tool_names: string[]
  reflected_tool_names: string[]
  feature_enabled: boolean
  feature_available: boolean
  feature_unavailable_reason: string
}

export interface BrowserDevToolsStatus {
  server: string
  running: boolean
  connected: boolean
  state: string
  startup_phase: string
  tool_count: number
  last_error: string
  unhealthy_reason: string
  connected_at: string | null
}

export interface BrowserExtensionStatus {
  installed: boolean
  paired: boolean
  installed_version: string
  bundled_version: string
  update_available: boolean
  server_running: boolean
  connected: boolean
  connected_version: string
  last_error: string
}

export interface BrowserUseDiagnostics {
  skill_enabled: boolean
  skill_available: boolean
  skill_unavailable_reason: string
  chrome_found: boolean
  chrome_executable: string
  /** The built-in browser server that the extension connects to. */
  devtools: BrowserDevToolsStatus
  extension?: BrowserExtensionStatus
  last_error: string
}

export interface BrowserExtensionInstallResult {
  /** The folder to pick in Chrome's "Load unpacked" dialog. */
  folder: string
  version: string
  folder_opened: boolean
  extensions_page_opened: boolean
  status: BrowserUseDiagnostics
}

export interface DiagnosticsSummary {
  backend: {
    name: string
    version: string
    port: number
    runtime_dir: string
    database: {
      path: string
      exists: boolean
      size_bytes: number
    }
  }
  scheduler: {
    running: boolean
    stopping?: boolean
    inflight_tasks?: number
    startup_error?: string
  }
  telegram: {
    configured: boolean
    running: boolean
    startup_error: string
  }
  mcp: {
    enabled: boolean
    configured_servers: number
    runtime_servers: number
    connected_servers: number
    unhealthy_servers: number
    state?: string
    startup_error?: string
  }
  browser: {
    chrome_found: boolean
    devtools_state: string
    devtools_connected: boolean
    last_error: string
  }
  sandbox: SandboxStatus
}

export interface ObservabilityUsage {
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  image_tokens: number
  total_tokens: number
  source: string
}

export interface ObservabilitySummary {
  total_runs: number
  successful_runs: number
  success_rate: number
  failed_runs: number
  total_tokens: number
  estimated_cost_usd: number
  average_duration_ms: number
  tool_error_count: number
  storage_path: string
  token_usage_over_time: Array<{ day: string; runs: number; tokens: number; average_duration_ms: number }>
  duration_trend: Array<{ day: string; runs: number; tokens: number; average_duration_ms: number }>
  top_error_reasons: Array<{ reason: string; count: number }>
  top_failing_tools: Array<{ tool_name: string; count: number }>
  model_usage: Array<{ model: string; provider: string; runs: number; tokens: number }>
  storage_metrics: Record<string, unknown>
  runtime_metrics: Record<string, unknown>
  field_classification: Record<string, unknown>
}

export interface ObservabilityRun {
  run_id: string
  conversation_id: string
  message_id: string
  source: string
  model: string
  provider: string
  status: string
  failure_reason: string
  failure_pattern: string
  started_at: string
  finished_at: string
  duration_ms: number
  user_message: string
  final_output: string
  input_tokens: number
  output_tokens: number
  reasoning_tokens: number
  cached_tokens: number
  image_tokens: number
  total_tokens: number
  usage_source: string
  estimated_cost_usd: number
  cost_source: string
  tool_count: number
  tool_error_count: number
  event_count: number
  metadata_json?: string
}

export interface ObservabilityEvent {
  event_id: string
  run_id: string
  conversation_id: string
  message_id: string
  event_type: string
  level: string
  status: string
  source: string
  model: string
  provider: string
  tool_name: string
  error_code: string
  error_message: string
  duration_ms: number
  input: unknown
  output: unknown
  tokens: ObservabilityUsage | null
  metadata: Record<string, unknown>
  created_at: string
}

export interface ObservabilityError {
  error_id: string
  run_id: string
  conversation_id: string
  message_id: string
  level: string
  logger_name: string
  module: string
  error_type: string
  message: string
  traceback: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface ObservabilityReplay {
  replay_id: string
  source_run_id: string
  replay_run_id: string
  conversation_id: string
  status_change: string
  duration_delta_ms: number
  token_delta: number
  tool_sequence_diff: string
  failure_reason_diff: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface ObservabilityRunDetail extends ObservabilityRun {
  events: ObservabilityEvent[]
  errors: ObservabilityError[]
  replays: ObservabilityReplay[]
  tool_sequence: string[]
}

export interface ObservabilitySupportMode {
  enabled: boolean
  expires_at_epoch: number
  remaining_seconds: number
}

export interface ObservabilityBackendLog {
  path: string
  exists: boolean
  size_bytes?: number
  lines: string[]
  truncated: boolean
  error?: string
}

export interface ObservabilityReplayResult {
  source_run_id: string
  replay_run_id: string
  conversation_id: string
  status: string
  summary: string
  errors: string[]
  comparison: ObservabilityReplay
}

export interface ObservabilityDebugBundle {
  filename: string
  run_id: string
  size_bytes: number
  encrypted: boolean
}

export interface DataDeletionResult {
  ok: boolean
  deleted: Record<string, number>
  deleted_at: string
}

export interface AgentSettings {
  permissions_config_path?: string
  llm: {
    provider: 'openai' | 'gemini' | 'codex'
    model_name: string
    reasoning_effort: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra'
    max_iterations_per_turn: number
    max_turn_seconds: number
    max_llm_call_seconds: number
  }
  speech_to_text: {
    provider: SpeechToTextProvider
  }
  mcp: {
    enabled: boolean
    servers: MCPServerConfig[]
  }
  browser: {
    screenshots_dir: string
    extension_port?: number
    allowed_domains: string[]
  }
  memory: {
    enabled: boolean
    auto_learn: boolean
    retrieval_limit: number
    max_injected_chars: number
    min_relevance_score: number
  }
  tools: {
    skills: Record<string, boolean>
  }
  permissions: {
    mode: 'default' | 'full_access' | 'auto_review' | 'custom'
    confirmations: ConfirmationSettings
    blocked_roots: string[]
    path_rules: PathRule[]
    app_rules: AppRule[]
    allow_delete: boolean
    dangerous_actions_require_confirm: boolean
    allow_screen_fallback: boolean
    custom_profile?: PermissionProfile
  }
  sandbox: {
    enabled: boolean
    mode: 'off' | 'disabled' | 'auto' | 'enforce' | 'host' | 'docker' | 'local_restricted'
    default_profile: 'standard' | 'untrusted' | 'host_required'
    default_write_strategy: 'discard' | 'copy_out' | 'direct_rw'
    allowed_bind_roots: string[]
    blocked_bind_roots: string[]
    preserve_artifacts_days: number
    resources: {
      timeout_seconds: number
      memory_mb: number
      cpus: number
      pids: number
      max_output_bytes: number
      max_copy_in_bytes: number
      max_copy_out_bytes: number
    }
    network: {
      default: 'deny' | 'allow_with_approval' | 'allow'
    }
    docker: {
      enabled: boolean
      image: string
      pull_policy: 'never' | 'missing' | 'always'
      read_only_root: boolean
      no_new_privileges: boolean
    }
    local_restricted: {
      enabled: boolean
    }
  }
  identity: {
    agent_name: string
    user_name: string
    user_identity: string
    communication_style: string
  }
  settings_version: string
  available_skills: SkillDescriptor[]
  api_keys: {
    has_openai_key: boolean
    has_google_key: boolean
    has_tavily_key: boolean
    has_telegram_bot_token: boolean
    has_telegram_allowlist: boolean
  }
  telegram_allowed_user_ids: string
  telegram_allowed_chat_ids: string
}

export interface ModelOptions {
  providers: Array<{
    id: AgentSettings['llm']['provider']
    label: string
    models: string[]
  }>
}

/** "auto" uses whichever OpenAI or Google key is saved. */
export type SpeechToTextProvider = 'auto' | 'openai' | 'gemini'

export interface SpeechToTextStatus {
  provider: SpeechToTextProvider
  /** The provider voice input uses now; empty when it has no key. */
  active_provider: '' | 'openai' | 'gemini'
  model: string
  configured: boolean
}

export interface SpeechToTextTranscription {
  text: string
}

export interface WorkspaceInstructions {
  path: string
  content: string
}

export interface SettingsUpdatePayload {
  expected_settings_version?: string
  llm?: AgentSettings['llm']
  speech_to_text?: AgentSettings['speech_to_text']
  mcp?: AgentSettings['mcp']
  browser?: AgentSettings['browser']
  memory?: AgentSettings['memory']
  tools?: AgentSettings['tools']
  permissions?: AgentSettings['permissions']
  sandbox?: AgentSettings['sandbox']
  identity?: AgentSettings['identity']
  openai_api_key?: string
  google_api_key?: string
  tavily_api_key?: string
  telegram_bot_token?: string
  telegram_allowed_user_ids?: string
  telegram_allowed_chat_ids?: string
  model_provider?: string
  model_name?: string
  reasoning_effort?: string
  controller_permission_mode?: string
}

export interface SandboxStatus {
  execution_environment?: ExecutionEnvironment
  enabled: boolean
  mode: AgentSettings['sandbox']['mode']
  default_profile: AgentSettings['sandbox']['default_profile']
  default_network: AgentSettings['sandbox']['network']['default']
  default_write_strategy: AgentSettings['sandbox']['default_write_strategy']
  selected_backend: string
  isolation: 'strong' | 'advisory' | 'none' | string
  reason_code: string
  reason: string
  representative_shell: string
  representative_profile: string
  fallback_backend: string
  fallback_isolation: string
  fallback_reason_code: string
  fallback_reason: string
  python_import_support: 'probed' | 'builtin_fallback' | 'unavailable' | string
  python_import_detail: string
  backends: Record<string, {
    backend: string
    enabled: boolean
    available: boolean
    security_label: string
    network_enforcement: string
    version?: string
    reason?: string
  }>
}

export interface AppEntry {
  alias: string
  display_name: string
  exe_paths: string[]
  added_at: string
}

export interface ControllerPolicy {
  mode: string
  permitted_roots: string[]
  blocked_roots: string[]
  allow_delete: boolean
  dangerous_actions_require_confirm: boolean
  allowlisted_apps: AppEntry[]
}

export interface ExecutionEnvironment {
  backend: string
  summary: string
  workspace: string
  execution_workspace: string
  shell: string
  image?: string
  filesystem: string
  network: string
  writes: string
  approval_required: boolean
  reason: string
  scope: string
}

export interface ApprovalTicket {
  review_decision?: 'approve' | 'deny' | 'ask' | ''
  review_reason?: string
  id: string
  conversation_id: string
  action_type: string
  tool_name: string
  target_path: string
  target_app: string
  risk_level: string
  reason: string
  action_description: string
  payload: {
    args?: {
      env_keys?: string[]
      sandbox?: {
        execution_environment?: ExecutionEnvironment
        backend?: string
        selected_backend?: string
        security_label?: string
        env_inheritance?: string
        explicit_env_keys?: string[]
        blocked_env_keys?: string[]
      }
    }
  }
  payload_hash: string
  status: string
  created_at: string
  resolved_at: string
  resolved_by: string
  execution_result: string
}

export interface ApprovalEvent {
  execution_environment?: ExecutionEnvironment
  ticket_id: string
  action: string
  reason: string
  decision?: 'approve' | 'deny' | 'ask'
  review_reason?: string
  resolved_by?: string
}

export interface PlanStep {
  step_id: string
  description: string
  tool_hints: string[]
  depends_on: string[]
  success_criteria: string
  status: 'pending' | 'active' | 'done' | 'failed' | 'retrying'
}

export interface PlanEvent {
  plan_id: string
  original_message: string
  steps: PlanStep[]
}

export interface StepEvent {
  step_id: string
  description?: string
  status?: string
  verified?: boolean
  detail?: string
  reason?: string
  attempt?: number
}

export interface AccessGrantRequiredEvent {
  ticket_id: string
  target_type: string
  target_identifier: string
  display_name: string
  action_context: string
  requested_access?: 'read' | 'write' | 'delete' | 'launch' | string
}
