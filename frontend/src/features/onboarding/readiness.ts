import type { OpenAIAccountStatus } from '../../lib/api/settings'
import type { AgentSettings, BrowserUseDiagnostics, DiagnosticsSummary } from '../../lib/api/types'
import { providerLabel } from '../settings/settingsConfig'
import { PERMISSION_MODE_COPY, type SettingsPageId } from '../settings/settingsCopy'

/** `ready` needs nothing; `action` needs the user; `problem` failed a check; `checking` is waiting on one. */
export type StepState = 'ready' | 'action' | 'problem' | 'checking'

export interface GuideStep {
  id: string
  title: string
  detail: string
  state: StepState
  /** Settings page that resolves or explains this step. */
  settingsPage?: SettingsPageId
  actionLabel?: string
}

/**
 * The guide shows in an empty new chat: always while no model is connected,
 * otherwise only before the first conversation exists and until it is hidden.
 */
export function shouldShowGuide({
  inEmptyNewChat,
  loaded,
  modelReady,
  dismissed,
  conversationsLoaded,
  conversationCount,
}: {
  inEmptyNewChat: boolean
  loaded: boolean
  modelReady: boolean
  dismissed: boolean
  conversationsLoaded: boolean
  conversationCount: number
}): boolean {
  if (!inEmptyNewChat || !loaded) return false
  if (!modelReady) return true
  return !dismissed && conversationsLoaded && conversationCount === 0
}

export const EXAMPLE_PROMPT = 'Look at the files in my workspace folder and give me a short summary of what is there. Do not change anything.'

/** Whether the selected provider has the credential it needs, from saved key status or the account check. */
function modelStep(settings: AgentSettings, account: OpenAIAccountStatus | null): GuideStep {
  const provider = settings.llm.provider
  const label = providerLabel(provider)
  const base = { id: 'model', title: 'Connect a model', settingsPage: 'apiKeys' as const }
  if (provider === 'codex') {
    if (!account) return { ...base, state: 'checking', detail: 'Checking your OpenAI account…' }
    return account.connected
      ? { ...base, state: 'ready', detail: `${label} · ${settings.llm.model_name}` }
      : { ...base, state: 'action', detail: 'Sign in with ChatGPT to use your OpenAI account.', actionLabel: 'Sign in' }
  }
  const hasKey = provider === 'gemini' ? settings.api_keys?.has_google_key : settings.api_keys?.has_openai_key
  return hasKey
    ? { ...base, state: 'ready', detail: `${label} · ${settings.llm.model_name}` }
    : { ...base, state: 'action', detail: `Add an API key for ${label}, or choose another provider.`, actionLabel: 'Add key' }
}

export function requiredSteps({
  settings,
  account,
  workspaceDir,
}: {
  settings: AgentSettings
  account: OpenAIAccountStatus | null
  workspaceDir: string
}): GuideStep[] {
  const mode = PERMISSION_MODE_COPY[settings.permissions?.mode] ?? PERMISSION_MODE_COPY.default
  return [
    modelStep(settings, account),
    {
      id: 'workspace',
      title: 'Pick a working folder',
      state: 'ready',
      detail: workspaceDir
        ? `Working in ${workspaceDir}.`
        : 'Using the default workspace. Choose a project folder with the folder button beside the message box.',
    },
    {
      id: 'permissions',
      title: `Permissions: ${mode.title}`,
      state: 'ready',
      detail: mode.summary,
      settingsPage: 'permissions',
      actionLabel: 'Review',
    },
  ]
}

/**
 * Optional capabilities appear only once the user has turned them on, and
 * their state comes from live diagnostics rather than the toggle itself.
 */
export function optionalSteps({
  settings,
  summary,
  browser,
}: {
  settings: AgentSettings
  summary: DiagnosticsSummary | null
  browser: BrowserUseDiagnostics | null
}): GuideStep[] {
  const steps: GuideStep[] = []

  if (settings.tools?.skills?.['browser-use']) {
    const base = { id: 'browser', title: 'Browser control', settingsPage: 'browser' as const }
    const extension = browser?.extension
    if (!browser) steps.push({ ...base, state: 'checking', detail: 'Checking Chrome…' })
    else if (!browser.chrome_found) steps.push({ ...base, state: 'problem', detail: 'Chrome was not found on this computer.', actionLabel: 'Details' })
    else if (extension?.connected) steps.push({ ...base, state: 'ready', detail: 'Chrome is connected through the Monaw extension.' })
    else if (extension?.installed) steps.push({ ...base, state: 'action', detail: 'Load the Monaw extension in Chrome to finish connecting.', actionLabel: 'Finish setup' })
    else steps.push({ ...base, state: 'action', detail: 'Set up the Monaw extension so the agent can use Chrome.', actionLabel: 'Set up' })
  }

  // Automatic mode uses Docker only when it happens to be available; Docker is
  // the user's choice only when commands must run in a container.
  const requiresDocker = settings.sandbox?.mode === 'docker' || settings.sandbox?.mode === 'enforce'
  if (settings.sandbox?.enabled && requiresDocker) {
    const base = { id: 'docker', title: 'Docker isolation', settingsPage: 'sandbox' as const }
    const docker = summary?.sandbox?.backends?.docker
    if (!summary) steps.push({ ...base, state: 'checking', detail: 'Checking Docker…' })
    else if (docker?.available) steps.push({ ...base, state: 'ready', detail: 'Docker is available for isolated commands.' })
    else {
      steps.push({
        ...base,
        state: 'problem',
        detail: `${docker?.reason || 'Docker is not available.'} Commands are blocked until it is.`,
        actionLabel: 'Details',
      })
    }
  }

  if (settings.api_keys?.has_telegram_bot_token) {
    const base = { id: 'telegram', title: 'Telegram', settingsPage: 'apiKeys' as const }
    const telegram = summary?.telegram
    if (!telegram) steps.push({ ...base, state: 'checking', detail: 'Checking the Telegram bot…' })
    else if (telegram.startup_error) steps.push({ ...base, state: 'problem', detail: telegram.startup_error, actionLabel: 'Details' })
    else if (!telegram.configured) steps.push({ ...base, state: 'action', detail: 'Add the user or chat IDs allowed to message the bot.', actionLabel: 'Add IDs' })
    else if (telegram.running) steps.push({ ...base, state: 'ready', detail: 'The Telegram bot is running.' })
    else steps.push({ ...base, state: 'problem', detail: 'The Telegram bot is configured but not running.', actionLabel: 'Details' })
  }

  const enabledServers = settings.mcp?.enabled ? (settings.mcp.servers ?? []).filter((server) => server.enabled).length : 0
  if (enabledServers > 0) {
    const base = { id: 'mcp', title: 'External connections (MCP)', settingsPage: 'tools' as const }
    const mcp = summary?.mcp
    if (!mcp) steps.push({ ...base, state: 'checking', detail: 'Checking connections…' })
    else if (mcp.startup_error) steps.push({ ...base, state: 'problem', detail: mcp.startup_error, actionLabel: 'Details' })
    else if (mcp.connected_servers >= enabledServers) {
      steps.push({ ...base, state: 'ready', detail: `${mcp.connected_servers} of ${enabledServers} connected.` })
    } else {
      steps.push({
        ...base,
        state: 'problem',
        detail: `${mcp.connected_servers} of ${enabledServers} connected${mcp.unhealthy_servers ? `, ${mcp.unhealthy_servers} unhealthy` : ''}.`,
        actionLabel: 'Details',
      })
    }
  }

  return steps
}
