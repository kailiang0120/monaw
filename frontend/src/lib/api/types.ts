export interface Conversation {
  id: string
  title: string
  created_at: string
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
  estimator: string
  breakdown: {
    key: string
    label: string
    tokens: number
    percentage: number
    kind: 'used' | 'reserved' | 'free'
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
  }[]
  created_at: string
}

export interface MessagesResponse {
  messages: SavedMessage[]
  has_more: boolean
}

export interface AccessGrantTicket {
  id: string
  conversation_id: string
  target_type: string
  target_identifier: string
  display_name: string
  action_context: string
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
  description: string
  version: string
  enabled_by_default: boolean
  enabled: boolean
  available: boolean
  always: boolean
  unavailable_reason: string
  load_error?: string
  tier: 'recommended' | 'optional'
  recommended: boolean
}

export interface MCPServerConfig {
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
  login_capable: boolean
  skill_enabled: boolean
  skill_available: boolean
  skill_unavailable_reason: string
  feature_enabled: boolean
  feature_available: boolean
  feature_unavailable_reason: string
}

export interface BrowserUseProfile {
  name: string
  directory: string
}

export interface BrowserUseDiagnostics {
  available: boolean
  skill_enabled: boolean
  skill_available: boolean
  skill_unavailable_reason: string
  session_active: boolean
  preferred_mode: 'auto' | 'managed' | 'system'
  current_mode: '' | 'managed' | 'system'
  current_system_connection: '' | 'attach' | 'launch'
  fallback_enabled: boolean
  last_error: string
  system_connection_strategy: 'auto' | 'attach' | 'launch'
  system_cdp_url: string
  managed_profile_dir: string
  downloads_dir: string
  screenshots_dir: string
  traces_dir: string
  system_profile_directory: string
  chrome_executable: string
  available_system_profiles: BrowserUseProfile[]
  current_page: {
    target_id: string
    url: string
    title: string
  } | null
  tab_count: number
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
  }
  browser: {
    available: boolean
    session_active: boolean
    preferred_mode: string
    current_mode: string
    last_error: string
  }
  sandbox: SandboxStatus
}

export interface EvaluationRun {
  run_id: string
  conversation_id: string
  model: string
  provider: string
  started_at: string
  finished_at: string
  success: boolean
  error: string
  entry_count: number
  tool_count: number
  total_tokens: number
  total_cost_usd: number
  last_user_message: string
  final_assistant_message: string
  source: string
}

export interface EvaluationReplayResult {
  source_run_id: string
  conversation_id: string
  status: string
  summary: string
  errors: string[]
}

export type MemoryCategory = 'preference' | 'behavior' | 'fact' | 'workflow' | 'project' | 'reflection'
export type MemoryStatus = 'active' | 'archived'
export type MemoryReviewState = 'new' | 'reviewed'
export type MemoryKind = 'fact' | 'reflection'

export interface MemoryRecord {
  id: string
  content: string
  category: MemoryCategory
  status: MemoryStatus
  review_state: MemoryReviewState
  confidence: number
  importance: number
  kind: MemoryKind
  source: string
  source_conversation_id: string
  source_message_id: number | null
  created_at: string
  updated_at: string
  last_used_at: string
  use_count: number
}

export interface MemorySectionRecord extends MemoryRecord {
  title: string
  body: string
}

export interface MemoryFileRecord {
  category: MemoryCategory
  raw_markdown: string
  sections: MemorySectionRecord[]
  updated_at: string
}

export interface MemorySearchRecord extends MemoryRecord {
  score: number
  score_breakdown: {
    exact: number
    token: number
    category: number
    recency: number
    importance: number
    use: number
  }
}

export interface MemoryAuditRecord {
  id: string
  memory_id: string | null
  action: string
  reason: string
  source_conversation_id: string
  candidate_content: string
  created_at: string
}

export interface MemoryStats {
  total: number
  active: number
  archived: number
  new: number
  reviewed: number
  fact: number
  reflection: number
  candidates: number
  unresolved_candidates: number
  short_term: number
  personalities: number
  curated_sessions: number
  audit_events: number
  archived_messages: number
  episodes: number
  active_checkpoints: number
  profile_fields: number
  memory_root: string
  categories: Partial<Record<MemoryCategory, number>>
}

export interface MemoryProfileField {
  field: string
  value: string
  privacy_level: 'normal' | 'private' | 'sensitive'
  confidence: number
  review_state: MemoryReviewState
  source_conversation_id: string
  source_message_id: number | null
  updated_at: string
}

export interface MemoryCandidate {
  id: string
  kind: MemoryKind
  content: string
  category: MemoryCategory
  confidence: number
  importance: number
  status: 'new' | 'approved' | 'rejected'
  reason: string
  source_conversation_id: string
  source_message_id: number | null
  created_at: string
  updated_at: string
}

export interface MemoryCheckpoint {
  id: string
  scope: string
  status: string
  conversation_id: string
  project: string
  app_name: string
  goal: string
  last_known_state: string
  next_action: string
  blocker: string
  browser_url: string
  browser_title: string
  workspace_path: string
  files_touched: string[]
  commands_run: string[]
  expires_at: string
  source_refs: Record<string, unknown>[]
  created_at: string
  updated_at: string
}

export interface MemoryEpisode {
  id: string
  conversation_id: string
  channel: string
  project: string
  task_type: string
  summary: string
  decisions: unknown[]
  artifacts: unknown[]
  errors: unknown[]
  fixes: unknown[]
  open_questions: unknown[]
  follow_ups: unknown[]
  source_message_start_id: number | null
  source_message_end_id: number | null
  tool_call_ids: number[]
  created_at: string
  updated_at: string
}

export interface AgentSettings {
  llm: {
    provider: 'openai' | 'deepseek' | 'gemini'
    model_name: string
    reasoning_effort: 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
    vision_fallback_enabled: boolean
    vision_fallback_model: string
    max_iterations_per_turn: number
    max_turn_seconds: number
    max_llm_call_seconds: number
  }
  speech_to_text: {
    engine: 'local' | 'cloud'
    local_model: string
    cloud_provider: 'gemini'
    cloud_model: string
  }
  mcp: {
    enabled: boolean
    servers: MCPServerConfig[]
  }
  browser: {
    mode: 'auto' | 'managed' | 'system'
    enable_system_fallback: boolean
    headless: boolean
    keep_alive: boolean
    system_connection_strategy: 'auto' | 'attach' | 'launch'
    system_cdp_url: string
    managed_profile_dir: string
    downloads_dir: string
    screenshots_dir: string
    traces_dir: string
    system_profile_directory: string
    allowed_domains: string[]
  }
  memory: {
    enabled: boolean
    auto_learn: boolean
    curate_on_session_close: boolean
    write_policy: 'off' | 'manual' | 'auto_with_review' | 'auto_reviewed'
    retrieval_limit: number
    max_injected_chars: number
    min_confidence: number
    min_relevance_score: number
    maintenance_cooldown_hours: number
  }
  tools: {
    skills: Record<string, boolean>
  }
  permissions: {
    mode: 'default' | 'full_access' | 'custom'
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
    mode: 'off' | 'disabled' | 'auto' | 'enforce' | 'docker' | 'local_restricted' | 'wsl'
    default_profile: 'standard' | 'untrusted' | 'project_write' | 'host_required'
    require_strong_for_untrusted: boolean
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
      max_workspace_mb: number
    }
    network: {
      default: 'deny' | 'allow_with_approval' | 'allow'
      allow_domains: string[]
    }
    docker: {
      enabled: boolean
      image: string
      extra_images: string[]
      pull_policy: 'never' | 'missing' | 'always'
      read_only_root: boolean
      no_new_privileges: boolean
    }
    local_restricted: {
      enabled: boolean
      use_job_object: boolean
      kill_process_tree_on_timeout: boolean
      strip_environment: boolean
    }
    wsl: {
      enabled: boolean
      distro: string
      note_network_isolation_is_advisory: boolean
    }
  }
  identity: {
    agent_name: string
    user_name: string
    user_identity: string
    communication_style: string
  }
  available_skills: SkillDescriptor[]
  api_keys: {
    has_openai_key: boolean
    has_deepseek_key: boolean
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
  vision_fallback_models: string[]
}

export interface SpeechToTextStatus {
  provider: 'faster-whisper'
  label: string
  engine: 'local' | 'cloud'
  cloud_provider: 'gemini'
  cloud_model: string
  cloud_configured: boolean
  model_id: string
  model_label: string
  model_size: string
  downloaded: boolean
  download_dir: string
  dependency_available: boolean
  loaded: boolean
}

export interface SpeechToTextTranscription {
  text: string
}

export interface WorkspaceInstructions {
  path: string
  content: string
}

export interface SettingsUpdatePayload {
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
  deepseek_api_key?: string
  deepseek_base_url?: string
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
  enabled: boolean
  mode: AgentSettings['sandbox']['mode']
  default_profile: AgentSettings['sandbox']['default_profile']
  default_network: AgentSettings['sandbox']['network']['default']
  default_write_strategy: AgentSettings['sandbox']['default_write_strategy']
  require_strong_for_untrusted: boolean
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

export interface ApprovalTicket {
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
  ticket_id: string
  action: string
  reason: string
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
}
