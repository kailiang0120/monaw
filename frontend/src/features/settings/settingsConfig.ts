import type { AgentSettings, ModelOptions } from '../../lib/api/types'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../../lib/identity'

export type ModelOptionsCatalog = ModelOptions

export const OPENAI_MODELS = ['gpt-5.6-luna']
export const GEMINI_MODELS = [
  'gemini-3.1-pro-preview',
  'gemini-3.1-flash-lite',
  'gemini-3.1-flash-lite-preview',
  'gemini-3-flash-preview',
]
export const FALLBACK_MODEL_OPTIONS: ModelOptionsCatalog = {
  providers: [
    { id: 'openai', label: 'OpenAI', models: OPENAI_MODELS },
    { id: 'gemini', label: 'Google', models: GEMINI_MODELS },
  ],
}
export const LLM_PROVIDERS: Array<AgentSettings['llm']['provider']> = FALLBACK_MODEL_OPTIONS.providers.map((provider) => provider.id)

export const OPENAI_REASONING_EFFORTS: Array<AgentSettings['llm']['reasoning_effort']> = [
  'none',
  'low',
  'medium',
  'high',
  'xhigh',
]
export const GEMINI_PRO_REASONING_EFFORTS: Array<AgentSettings['llm']['reasoning_effort']> = [
  'low',
  'medium',
  'high',
]
export const GEMINI_FLASH_REASONING_EFFORTS: Array<AgentSettings['llm']['reasoning_effort']> = [
  'minimal',
  'low',
  'medium',
  'high',
]
export const GEMINI_25_PRO_REASONING_EFFORTS: Array<AgentSettings['llm']['reasoning_effort']> = [
  'low',
  'medium',
  'high',
]
export const GEMINI_25_FLASH_REASONING_EFFORTS: Array<AgentSettings['llm']['reasoning_effort']> = [
  'none',
  'low',
  'medium',
  'high',
]

export function providerOptions(catalog: ModelOptionsCatalog = FALLBACK_MODEL_OPTIONS) {
  return catalog.providers.length > 0 ? catalog.providers : FALLBACK_MODEL_OPTIONS.providers
}

export function providerLabel(
  provider: AgentSettings['llm']['provider'],
  catalog: ModelOptionsCatalog = FALLBACK_MODEL_OPTIONS,
): string {
  return providerOptions(catalog).find((item) => item.id === provider)?.label
    ?? FALLBACK_MODEL_OPTIONS.providers.find((item) => item.id === provider)?.label
    ?? provider
}

export function modelsForProvider(
  provider: AgentSettings['llm']['provider'],
  catalog: ModelOptionsCatalog = FALLBACK_MODEL_OPTIONS,
): string[] {
  const catalogModels = providerOptions(catalog).find((item) => item.id === provider)?.models
  if (catalogModels && catalogModels.length > 0) return catalogModels
  return FALLBACK_MODEL_OPTIONS.providers.find((item) => item.id === provider)?.models
    ?? OPENAI_MODELS
}

export function reasoningEffortsForProvider(
  provider: AgentSettings['llm']['provider'],
  modelName = '',
): Array<AgentSettings['llm']['reasoning_effort']> {
  if (provider === 'gemini') {
    if (modelName.startsWith('gemini-3') && modelName.includes('flash')) return GEMINI_FLASH_REASONING_EFFORTS
    if (modelName.startsWith('gemini-3') && modelName.includes('pro')) return GEMINI_PRO_REASONING_EFFORTS
    if (modelName.startsWith('gemini-2.5') && modelName.includes('pro')) return GEMINI_25_PRO_REASONING_EFFORTS
    return GEMINI_25_FLASH_REASONING_EFFORTS
  }
  return OPENAI_REASONING_EFFORTS
}

function clampInteger(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return fallback
  return Math.min(max, Math.max(min, Math.trunc(parsed)))
}

export const PERMISSION_PROFILES = {
  default: {
    confirmations: { mutate: true, delete: true, launch_app: true, click: true, type: true },
    allow_delete: false,
    dangerous_actions_require_confirm: true,
    allow_screen_fallback: false,
  },
  full_access: {
    confirmations: { mutate: false, delete: true, launch_app: false, click: false, type: false },
    allow_delete: false,
    dangerous_actions_require_confirm: false,
    allow_screen_fallback: true,
  },
} as const

export const PERMISSION_MODE_HELP = {
  default: 'Reads any non-blocked path. Mutating actions still ask for approval.',
  full_access: 'Reads and writes any non-blocked path. Delete stays off until you enable it below.',
  custom: 'Uses your custom confirmation, path override, and app override settings.',
} as const

type PermissionProfile = NonNullable<AgentSettings['permissions']['custom_profile']>

function clonePermissionProfile(profile: PermissionProfile): PermissionProfile {
  return {
    confirmations: { ...profile.confirmations },
    blocked_roots: [...profile.blocked_roots],
    path_rules: profile.path_rules.map((rule) => ({ ...rule })),
    app_rules: profile.app_rules.map((rule) => ({
      ...rule,
      exe_paths: [...rule.exe_paths],
    })),
    allow_delete: profile.allow_delete,
    dangerous_actions_require_confirm: profile.dangerous_actions_require_confirm,
    allow_screen_fallback: profile.allow_screen_fallback,
  }
}

export function permissionProfileFromPermissions(
  permissions: AgentSettings['permissions'],
): PermissionProfile {
  return {
    confirmations: { ...permissions.confirmations },
    blocked_roots: [...permissions.blocked_roots],
    path_rules: permissions.path_rules.map((rule) => ({ ...rule })),
    app_rules: permissions.app_rules.map((rule) => ({
      ...rule,
      exe_paths: [...rule.exe_paths],
    })),
    allow_delete: permissions.allow_delete,
    dangerous_actions_require_confirm: permissions.dangerous_actions_require_confirm,
    allow_screen_fallback: permissions.allow_screen_fallback,
  }
}

export function applyPermissionProfile(
  permissions: AgentSettings['permissions'],
  profile: PermissionProfile,
): AgentSettings['permissions'] {
  const cloned = clonePermissionProfile(profile)
  return {
    ...permissions,
    ...cloned,
    custom_profile: cloned,
  }
}

export const emptyPathRule = () => ({
  path: '',
  read: true,
  write: true,
  delete: false,
  launch: false,
  require_confirmation: false,
  enabled: true,
})

export const emptyAppRule = () => ({
  alias: '',
  display_name: '',
  exe_paths: [] as string[],
  launch_allowed: true,
  uia_allowed: true,
  screen_fallback_allowed: false,
  require_confirmation: false,
  enabled: true,
})

export const emptyPermissionProfile = (): PermissionProfile => ({
  confirmations: { mutate: true, delete: true, launch_app: true, click: true, type: true },
  blocked_roots: [],
  path_rules: [],
  app_rules: [],
  allow_delete: false,
  dangerous_actions_require_confirm: true,
  allow_screen_fallback: false,
})

const emptyMCPServer = (): AgentSettings['mcp']['servers'][number] => ({
  name: '',
  enabled: true,
  transport: 'stdio',
  command: '',
  args: [],
  env: {},
  cwd: '',
  url: '',
  headers: {},
  startup_timeout_ms: 8000,
  call_timeout_ms: 30000,
  reconnect_on_unhealthy: true,
  allow_list: [],
  description: '',
})

const emptyIdentity = (): AgentSettings['identity'] => ({
  agent_name: DEFAULT_AGENT_NAME,
  user_name: '',
  user_identity: '',
  communication_style: '',
})

const emptyMemory = (): AgentSettings['memory'] => ({
  enabled: true,
  auto_learn: true,
  curate_on_session_close: true,
  write_policy: 'auto_with_review',
  retrieval_limit: 6,
  max_injected_chars: 2500,
  min_confidence: 0.75,
  min_relevance_score: 0.15,
  maintenance_cooldown_hours: 24,
})

const emptySpeechToText = (): AgentSettings['speech_to_text'] => ({
  engine: 'local',
  local_model: 'base',
  cloud_provider: 'gemini',
  cloud_model: 'gemini-2.5-flash',
})

const emptySandbox = (): AgentSettings['sandbox'] => ({
  enabled: true,
  mode: 'auto',
  default_profile: 'standard',
  require_strong_for_untrusted: true,
  default_write_strategy: 'copy_out',
  allowed_bind_roots: [],
  blocked_bind_roots: [],
  preserve_artifacts_days: 14,
  resources: {
    timeout_seconds: 120,
    memory_mb: 1024,
    cpus: 1,
    pids: 128,
    max_output_bytes: 1048576,
    max_workspace_mb: 1024,
  },
  network: {
    default: 'deny',
    allow_domains: [],
  },
  docker: {
    enabled: true,
    image: 'python:3.12-slim',
    extra_images: [],
    pull_policy: 'missing',
    read_only_root: true,
    no_new_privileges: true,
  },
  local_restricted: {
    enabled: true,
    use_job_object: true,
    kill_process_tree_on_timeout: true,
    strip_environment: true,
  },
  wsl: {
    enabled: false,
    distro: '',
    note_network_isolation_is_advisory: true,
  },
})

export const MCP_SERVER_TEMPLATES = {
  chromeDevToolsBrowserUrl9222: {
    label: 'Chrome DevTools (browserUrl :9222)',
    build: (): AgentSettings['mcp']['servers'][number] => ({
      ...emptyMCPServer(),
      name: 'Chrome-dev-tools',
      command: 'npx',
      args: ['-y', 'chrome-devtools-mcp@latest', '--browserUrl=http://127.0.0.1:9222'],
      call_timeout_ms: 90000,
      description: 'Connect to Chrome on localhost:9222. Launch Chrome separately with --remote-debugging-port=9222 and a non-default --user-data-dir.',
    }),
  },
  chromeDevToolsAutoConnect: {
    label: 'Chrome DevTools (autoConnect)',
    build: (): AgentSettings['mcp']['servers'][number] => ({
      ...emptyMCPServer(),
      name: 'Chrome-dev-tools',
      command: 'npx',
      args: ['-y', 'chrome-devtools-mcp@latest', '--autoConnect'],
      call_timeout_ms: 90000,
      description: 'Attach to a running Chrome 144+ session after remote debugging is enabled in chrome://inspect/#remote-debugging.',
    }),
  },
  filesystem: {
    label: 'Filesystem',
    build: (): AgentSettings['mcp']['servers'][number] => ({
      ...emptyMCPServer(),
      name: 'filesystem',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', ''],
      description: 'Expose a local directory tree over MCP.',
    }),
  },
  custom: {
    label: 'Custom stdio',
    build: (): AgentSettings['mcp']['servers'][number] => emptyMCPServer(),
  },
  streamableHttp: {
    label: 'Streamable HTTP',
    build: (): AgentSettings['mcp']['servers'][number] => ({
      ...emptyMCPServer(),
      name: 'http-mcp',
      transport: 'streamable_http',
      url: 'http://127.0.0.1:3000/mcp',
      description: 'Connect to an MCP server over Streamable HTTP with optional static headers.',
    }),
  },
} as const

export type MCPServerTemplateKey = keyof typeof MCP_SERVER_TEMPLATES

export const SKILL_GROUP_COPY = {
  recommended: {
    title: 'Recommended capabilities',
    description: 'Default-on tools the agent should rely on first.',
  },
  optional: {
    title: 'Optional capabilities',
    description: 'Extra surface area that should stay explicit.',
  },
} as const

export const SKILL_GUIDANCE: Record<string, string> = {
  'browser-use': 'Managed browser automation first, with system Chrome fallback for logged-in workflows.',
  'computer-use': 'Computer-use automation primitives for native Windows applications.',
  memory: 'Durable user preferences and workflow context with review before trust.',
  scheduling: 'Create cron, interval, and one-time agent tasks from chat or the scheduled tasks panel.',
  'background-check': 'Sanction and adverse-list screening with evidence capture. Enable only when needed.',
}

export function normalizeDraft(
  settings: AgentSettings,
  catalog: ModelOptionsCatalog = FALLBACK_MODEL_OPTIONS,
): AgentSettings {
  const providers = providerOptions(catalog).map((option) => option.id)
  const provider = providers.includes(settings.llm.provider) ? settings.llm.provider : 'openai'
  const modelOptions = modelsForProvider(provider, catalog)
  const modelName = modelOptions.includes(settings.llm.model_name) ? settings.llm.model_name : modelOptions[0]
  const reasoningOptions = reasoningEffortsForProvider(provider, modelName)
  const reasoningEffort = reasoningOptions.includes(settings.llm.reasoning_effort)
    ? settings.llm.reasoning_effort
    : reasoningOptions[0]
  const customProfile = settings.permissions.custom_profile
    ? clonePermissionProfile(settings.permissions.custom_profile)
    : settings.permissions.mode === 'custom'
      ? permissionProfileFromPermissions(settings.permissions)
      : emptyPermissionProfile()

  return {
    ...settings,
    settings_version: settings.settings_version || '',
    telegram_allowed_user_ids: (settings.telegram_allowed_user_ids ?? '').trim(),
    telegram_allowed_chat_ids: (settings.telegram_allowed_chat_ids ?? '').trim(),
    identity: {
      ...emptyIdentity(),
      ...(settings.identity ?? {}),
      agent_name: resolveAgentName(settings.identity?.agent_name),
      user_name: (settings.identity?.user_name ?? '').trim(),
      user_identity: (settings.identity?.user_identity ?? '').trim(),
      communication_style: (settings.identity?.communication_style ?? '').trim(),
    },
    mcp: {
      enabled: settings.mcp.enabled ?? true,
      servers: settings.mcp.servers,
    },
    memory: {
      ...emptyMemory(),
      ...(settings.memory ?? {}),
      retrieval_limit: clampInteger(settings.memory?.retrieval_limit, 6, 1, 20),
      max_injected_chars: clampInteger(settings.memory?.max_injected_chars, 2500, 500, 10000),
      min_confidence: Math.min(1, Math.max(0, Number(settings.memory?.min_confidence ?? 0.75))),
      min_relevance_score: Math.min(1, Math.max(0, Number(settings.memory?.min_relevance_score ?? 0.15))),
      maintenance_cooldown_hours: clampInteger(settings.memory?.maintenance_cooldown_hours, 24, 0, 168),
    },
    speech_to_text: {
      ...emptySpeechToText(),
      ...(settings.speech_to_text ?? {}),
      engine: settings.speech_to_text?.engine === 'cloud' ? 'cloud' : 'local',
      cloud_provider: 'gemini',
      cloud_model: settings.speech_to_text?.cloud_model === 'gemini-3.1-flash-lite'
        ? 'gemini-2.5-flash'
        : settings.speech_to_text?.cloud_model || 'gemini-2.5-flash',
      local_model: settings.speech_to_text?.local_model || 'base',
    },
    sandbox: {
      ...emptySandbox(),
      ...(settings.sandbox ?? {}),
      resources: {
        ...emptySandbox().resources,
        ...(settings.sandbox?.resources ?? {}),
      },
      network: {
        ...emptySandbox().network,
        ...(settings.sandbox?.network ?? {}),
      },
      docker: {
        ...emptySandbox().docker,
        ...(settings.sandbox?.docker ?? {}),
      },
      local_restricted: {
        ...emptySandbox().local_restricted,
        ...(settings.sandbox?.local_restricted ?? {}),
      },
      wsl: {
        ...emptySandbox().wsl,
        ...(settings.sandbox?.wsl ?? {}),
      },
    },
    llm: {
      ...settings.llm,
      provider,
      model_name: modelName,
      reasoning_effort: reasoningEffort,
      max_iterations_per_turn: clampInteger(settings.llm.max_iterations_per_turn, 40, 1, 500),
      max_turn_seconds: clampInteger(settings.llm.max_turn_seconds, 1800, 30, 14400),
      max_llm_call_seconds: clampInteger(settings.llm.max_llm_call_seconds, 300, 30, 1800),
    },
    permissions: settings.permissions.mode === 'custom'
      ? {
          ...applyPermissionProfile(settings.permissions, customProfile),
          mode: 'custom',
        }
      : {
          ...settings.permissions,
          custom_profile: customProfile,
        },
  }
}

export function formatUnavailableReason(reason: string): string {
  if (reason === 'missing_env') return 'Missing backend dependency or required environment.'
  if (reason === 'unsupported_os') return 'Not supported on this operating system.'
  return reason || 'Unavailable.'
}

export function duplicateMcpServerNames(servers: AgentSettings['mcp']['servers']): string[] {
  const counts = new Map<string, number>()
  servers.forEach((server) => {
    const name = server.name.trim()
    if (!name) return
    counts.set(name, (counts.get(name) ?? 0) + 1)
  })
  return Array.from(counts.entries())
    .filter(([, count]) => count > 1)
    .map(([name]) => name)
}

export function formatReasoningEffort(effort: AgentSettings['llm']['reasoning_effort']) {
  if (effort === 'minimal') return 'Minimal'
  if (effort === 'xhigh') return 'X-High'
  if (effort === 'max') return 'Max'
  return effort.charAt(0).toUpperCase() + effort.slice(1)
}
