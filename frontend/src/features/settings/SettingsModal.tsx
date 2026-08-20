import { Suspense, lazy, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  Activity,
  AlertCircle,
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  Container,
  Download,
  Folder,
  Globe2,
  KeyRound,
  Loader2,
  Mic,
  Plus,
  RotateCcw,
  Save,
  Search,
  Server,
  Shield,
  Trash2,
  UserRound,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react'
import { ConnectionPortalPanel } from './connectionPortals/ConnectionPortalPanel'
import {
  DEFAULT_CONNECTION_PORTAL,
  type ConnectionPortalId,
  type ConnectionSecretId,
  type ConnectionSecretStatuses,
  type ConnectionSecretValues,
} from './connectionPortals/connectionPortalConfig'
import { Dropdown } from '../../components/Dropdown'
import {
  Badge,
  ChoiceCard,
  FlagToggle,
  Note,
  NumberRow,
  PageHeader,
  ReadOnlyValue,
  SettingRow,
  SettingsCard,
  StackedRow,
  Switch,
  SwitchRow,
} from '../../components/ui/Panel'
import {
  BROWSER_COPY,
  CONFIRMATION_COPY,
  FOOTER_COPY,
  IDENTITY_COPY,
  MCP_COPY,
  MODEL_COPY,
  OVERRIDE_COPY,
  PAGE_COPY,
  PERMISSION_MODE_COPY,
  RISK_COPY,
  SANDBOX_COPY,
  SKILLS_COPY,
  type SettingsPageId,
} from './settingsCopy'
import {
  fetchBrowserUseDiagnostics,
  fetchMCPDiagnostics,
  resetBrowserUseSession,
  reconnectMCPServer,
} from '../../lib/api/diagnostics'
import {
  deleteSpeechToTextModel,
  downloadSpeechToTextModel,
  fetchModelOptions,
  fetchSettings,
  fetchSandboxStatus,
  fetchSpeechToTextStatus,
  fetchWorkspaceInstructions,
  offloadSpeechToTextModel,
  resetWorkspaceInstructions,
  resolveSandboxDockerImage,
  updateSettings,
  updateWorkspaceInstructions,
} from '../../lib/api/settings'
import { syncStoredApiKeysToBackend } from '../../lib/apiKeySync'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../../lib/identity'
import type {
  AgentSettings,
  BrowserUseDiagnostics,
  MCPServerDiagnostics,
  SandboxStatus,
  SpeechToTextStatus,
} from '../../lib/api/types'
import {
  MCP_SERVER_TEMPLATES,
  PERMISSION_PROFILES,
  SKILL_GROUP_COPY,
  applyPermissionProfile,
  duplicateMcpServerNames,
  emptyAppRule,
  emptyPathRule,
  FALLBACK_MODEL_OPTIONS,
  formatReasoningEffort,
  formatUnavailableReason,
  modelsForProvider,
  normalizeDraft,
  permissionProfileFromPermissions,
  providerLabel,
  providerOptions,
  reasoningEffortsForProvider,
  type ModelOptionsCatalog,
  type MCPServerTemplateKey,
} from './settingsConfig'

interface Props {
  onClose: () => void
  diagnosticsRefreshKey?: number
  memoryRefreshKey?: number
  observabilityRefreshKey?: number
}

type SettingsTab = SettingsPageId

const PERMISSION_MODE_LABEL: Record<AgentSettings['permissions']['mode'], string> = {
  default: 'Default',
  full_access: 'Full Access',
  custom: 'Custom',
}

const TAB_ICONS: Record<SettingsTab, LucideIcon> = {
  identity: UserRound,
  apiKeys: KeyRound,
  model: Bot,
  memory: Brain,
  skills: Wrench,
  browser: Globe2,
  mcp: Server,
  permissions: Shield,
  sandbox: Container,
  observability: Activity,
}

/**
 * Ten flat tabs is a list; grouped, it is a map. The grouping follows what a
 * user is trying to do — set the agent up, give it abilities, fence it in.
 */
const NAV_GROUPS: Array<{ id: string; label: string; items: SettingsTab[] }> = [
  { id: 'general', label: 'General', items: ['identity', 'apiKeys'] },
  { id: 'intelligence', label: 'Intelligence', items: ['model', 'memory'] },
  { id: 'capabilities', label: 'Capabilities', items: ['skills', 'browser', 'mcp'] },
  { id: 'safety', label: 'Safety', items: ['permissions', 'sandbox'] },
  { id: 'inspect', label: 'Inspect', items: ['observability'] },
]

const ALL_TABS: SettingsTab[] = NAV_GROUPS.flatMap((group) => group.items)

const MemorySettingsPanel = lazy(() =>
  import('./MemorySettingsPanel').then((module) => ({ default: module.MemorySettingsPanel })),
)
const ObservabilityPanel = lazy(() =>
  import('./ObservabilityPanel').then((module) => ({ default: module.ObservabilityPanel })),
)

/** The slice of settings that Save actually sends, used to detect edits. */
function draftSignature(
  draft: AgentSettings,
  telegramUserIds: string,
  telegramChatIds: string,
): string {
  return JSON.stringify({
    llm: draft.llm,
    speech_to_text: draft.speech_to_text,
    mcp: draft.mcp,
    browser: draft.browser,
    memory: draft.memory,
    tools: draft.tools,
    permissions: draft.permissions,
    sandbox: draft.sandbox,
    identity: draft.identity,
    telegramUserIds: telegramUserIds.trim(),
    telegramChatIds: telegramChatIds.trim(),
  })
}

export function SettingsModal({
  onClose,
  diagnosticsRefreshKey = 0,
  memoryRefreshKey = 0,
  observabilityRefreshKey = 0,
}: Props) {
  const [draft, setDraft] = useState<AgentSettings | null>(null)
  const [modelOptions, setModelOptions] = useState<ModelOptionsCatalog>(FALLBACK_MODEL_OPTIONS)
  const [activeTab, setActiveTab] = useState<SettingsTab>('model')
  const [navQuery, setNavQuery] = useState('')
  const [openaiKey, setOpenaiKey] = useState('')
  const [tavilyKey, setTavilyKey] = useState('')
  const [googleKey, setGoogleKey] = useState('')
  const [telegramBotToken, setTelegramBotToken] = useState('')
  const [telegramAllowedUserIds, setTelegramAllowedUserIds] = useState('')
  const [telegramAllowedChatIds, setTelegramAllowedChatIds] = useState('')
  const [dirtySecrets, setDirtySecrets] = useState<Set<ConnectionSecretId>>(() => new Set())
  const [showKey, setShowKey] = useState(false)
  const [activeConnectionPortal, setActiveConnectionPortal] = useState<ConnectionPortalId>(DEFAULT_CONNECTION_PORTAL)
  const [mcpTemplate, setMcpTemplate] = useState<MCPServerTemplateKey>('filesystem')
  const [browserDiagnostics, setBrowserDiagnostics] = useState<BrowserUseDiagnostics | null>(null)
  const [mcpDiagnostics, setMcpDiagnostics] = useState<Record<string, MCPServerDiagnostics>>({})
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatus | null>(null)
  const [resolvingDockerImage, setResolvingDockerImage] = useState(false)
  const [dockerResolveMessage, setDockerResolveMessage] = useState('')
  const [speechToTextStatus, setSpeechToTextStatus] = useState<SpeechToTextStatus | null>(null)
  const [customInstructions, setCustomInstructions] = useState('')
  const [customInstructionsPath, setCustomInstructionsPath] = useState('')
  const [customInstructionsSavedContent, setCustomInstructionsSavedContent] = useState('')
  const [savingCustomInstructions, setSavingCustomInstructions] = useState(false)
  const [resettingCustomInstructions, setResettingCustomInstructions] = useState(false)
  const [customInstructionsError, setCustomInstructionsError] = useState('')
  const [openMcpAdvanced, setOpenMcpAdvanced] = useState<Record<number, boolean>>({})
  const [reconnectingServer, setReconnectingServer] = useState('')
  const [resettingBrowserSession, setResettingBrowserSession] = useState(false)
  const [downloadingSpeechToText, setDownloadingSpeechToText] = useState(false)
  const [offloadingSpeechToText, setOffloadingSpeechToText] = useState(false)
  const [deletingSpeechToText, setDeletingSpeechToText] = useState(false)
  const [speechToTextError, setSpeechToTextError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [blockedRootInput, setBlockedRootInput] = useState('')
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null)
  const [diagOpen, setDiagOpen] = useState(false)
  const [browserAdvancedOpen, setBrowserAdvancedOpen] = useState(false)
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  const [baselineSignature, setBaselineSignature] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)

  const storeMcpDiagnostics = (items: MCPServerDiagnostics[]) => {
    const next: Record<string, MCPServerDiagnostics> = {}
    items.forEach((item) => {
      next[item.name] = item
    })
    setMcpDiagnostics(next)
  }

  const loadMcpDiagnostics = async (signal?: AbortSignal) => {
    try {
      storeMcpDiagnostics(await fetchMCPDiagnostics(signal))
    } catch {
      if (signal?.aborted) return
      setMcpDiagnostics({})
    }
  }

  const loadBrowserDiagnostics = async (signal?: AbortSignal) => {
    try {
      setBrowserDiagnostics(await fetchBrowserUseDiagnostics(signal))
    } catch {
      if (signal?.aborted) return
      setBrowserDiagnostics(null)
    }
  }

  const loadSandboxStatus = async (signal?: AbortSignal) => {
    try {
      setSandboxStatus(await fetchSandboxStatus(signal))
    } catch {
      if (signal?.aborted) return
      setSandboxStatus(null)
    }
  }

  const loadSpeechToTextStatus = async (signal?: AbortSignal) => {
    try {
      setSpeechToTextStatus(await fetchSpeechToTextStatus(signal))
      setSpeechToTextError('')
    } catch (error) {
      if (signal?.aborted) return
      setSpeechToTextStatus(null)
      setSpeechToTextError(error instanceof Error ? error.message : 'Speech-to-text status could not be loaded.')
    }
  }

  const loadWorkspaceInstructions = async (signal?: AbortSignal) => {
    try {
      const instructions = await fetchWorkspaceInstructions(signal)
      setCustomInstructions(instructions.content)
      setCustomInstructionsSavedContent(instructions.content)
      setCustomInstructionsPath(instructions.path)
      setCustomInstructionsError('')
    } catch (error) {
      if (signal?.aborted) return
      setCustomInstructions('')
      setCustomInstructionsSavedContent('')
      setCustomInstructionsPath('')
      setCustomInstructionsError(error instanceof Error ? error.message : 'Custom instructions could not be loaded.')
    }
  }

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    const signal = controller.signal

    const load = async () => {
      const modelOptionsPromise = fetchModelOptions(signal).catch(() => FALLBACK_MODEL_OPTIONS)
      modelOptionsPromise.then((loadedModelOptions) => {
        if (cancelled) return
        setModelOptions(loadedModelOptions)
        setDraft((current) => (current ? normalizeDraft(current, loadedModelOptions) : current))
      })

      let settings: AgentSettings | null = null
      try {
        settings = normalizeDraft(await fetchSettings(signal))
        if (!cancelled) {
          setLoadError('')
          setDraft(settings)
          setTelegramAllowedUserIds(settings.telegram_allowed_user_ids)
          setTelegramAllowedChatIds(settings.telegram_allowed_chat_ids)
          setBaselineSignature(draftSignature(
            settings,
            settings.telegram_allowed_user_ids,
            settings.telegram_allowed_chat_ids,
          ))
        }
      } catch (error) {
        if (signal.aborted) return
        console.error('[settings] Failed to load settings', error)
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : 'Settings could not be loaded.')
        }
        return
      }

      void Promise.all([
        loadMcpDiagnostics(signal),
        loadBrowserDiagnostics(signal),
        loadSandboxStatus(signal),
        loadSpeechToTextStatus(signal),
        loadWorkspaceInstructions(signal),
      ])

      if (window.electronAPI) {
        try {
          const credentialStatus = await syncStoredApiKeysToBackend()
          if (cancelled) return
          setDirtySecrets(new Set())
          setDraft((current) => current ? {
            ...current,
            api_keys: {
              ...current.api_keys,
              has_openai_key: current.api_keys.has_openai_key || credentialStatus.openai,
              has_google_key: current.api_keys.has_google_key || credentialStatus.google,
              has_tavily_key: current.api_keys.has_tavily_key || credentialStatus.tavily,
              has_telegram_bot_token:
                current.api_keys.has_telegram_bot_token || credentialStatus.telegramBot,
            },
          } : current)
        } catch {
          // Stored key sync should not prevent the settings UI from opening.
        }
      }
    }
    load()

    return () => {
      cancelled = true
      controller.abort()
    }
  }, [])

  useEffect(() => {
    if (!diagnosticsRefreshKey) return
    const controller = new AbortController()
    void Promise.all([
      loadMcpDiagnostics(controller.signal),
      loadBrowserDiagnostics(controller.signal),
      loadSandboxStatus(controller.signal),
    ])
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diagnosticsRefreshKey])

  // Switching pages should start you at the top, not halfway down the last one.
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0
  }, [activeTab])

  const updateDraft = (updater: (current: AgentSettings) => AgentSettings) => {
    setDraft((current) => (current ? updater(current) : current))
  }

  const updateLlmNumber = (
    key: 'max_iterations_per_turn' | 'max_turn_seconds' | 'max_llm_call_seconds',
    value: string,
    min: number,
    max: number,
  ) => {
    const parsed = Number(value)
    const nextValue = Number.isFinite(parsed)
      ? Math.min(max, Math.max(min, Math.trunc(parsed)))
      : min
    updateDraft((current) => ({
      ...current,
      llm: { ...current.llm, [key]: nextValue },
    }))
  }

  const updateMcpServer = (
    index: number,
    updater: (server: AgentSettings['mcp']['servers'][number]) => AgentSettings['mcp']['servers'][number],
  ) => {
    updateDraft((current) => {
      const servers = [...current.mcp.servers]
      servers[index] = updater(servers[index])
      return { ...current, mcp: { ...current.mcp, servers } }
    })
  }

  const handleReconnectServer = async (name: string) => {
    if (!name) return
    setReconnectingServer(name)
    try {
      const status = await reconnectMCPServer(name)
      setMcpDiagnostics((current) => ({ ...current, [name]: status }))
    } finally {
      setReconnectingServer('')
    }
  }

  const handleResetBrowserSession = async () => {
    setResettingBrowserSession(true)
    try {
      setBrowserDiagnostics(await resetBrowserUseSession())
    } finally {
      setResettingBrowserSession(false)
    }
  }

  const handleDownloadSpeechToText = async () => {
    setDownloadingSpeechToText(true)
    setSpeechToTextError('')
    try {
      setSpeechToTextStatus(await downloadSpeechToTextModel())
    } catch (error) {
      setSpeechToTextError(error instanceof Error ? error.message : 'Failed to download speech-to-text model.')
    } finally {
      setDownloadingSpeechToText(false)
    }
  }

  const handleOffloadSpeechToText = async () => {
    setOffloadingSpeechToText(true)
    setSpeechToTextError('')
    try {
      setSpeechToTextStatus(await offloadSpeechToTextModel())
    } catch (error) {
      setSpeechToTextError(error instanceof Error ? error.message : 'Failed to offload speech-to-text model.')
    } finally {
      setOffloadingSpeechToText(false)
    }
  }

  const handleDeleteSpeechToText = async () => {
    setDeletingSpeechToText(true)
    setSpeechToTextError('')
    try {
      setSpeechToTextStatus(await deleteSpeechToTextModel())
    } catch (error) {
      setSpeechToTextError(error instanceof Error ? error.message : 'Failed to delete speech-to-text model.')
    } finally {
      setDeletingSpeechToText(false)
    }
  }

  const handleSaveCustomInstructions = async () => {
    setSavingCustomInstructions(true)
    setCustomInstructionsError('')
    try {
      const instructions = await updateWorkspaceInstructions(customInstructions)
      setCustomInstructions(instructions.content)
      setCustomInstructionsSavedContent(instructions.content)
      setCustomInstructionsPath(instructions.path)
    } catch (error) {
      setCustomInstructionsError(error instanceof Error ? error.message : 'Failed to save custom instructions.')
    } finally {
      setSavingCustomInstructions(false)
    }
  }

  const handleResetCustomInstructions = async () => {
    setResettingCustomInstructions(true)
    setCustomInstructionsError('')
    try {
      const instructions = await resetWorkspaceInstructions()
      setCustomInstructions(instructions.content)
      setCustomInstructionsSavedContent(instructions.content)
      setCustomInstructionsPath(instructions.path)
    } catch (error) {
      setCustomInstructionsError(error instanceof Error ? error.message : 'Failed to reset custom instructions.')
    } finally {
      setResettingCustomInstructions(false)
    }
  }

  const chooseDirectory = async (currentPath: string) => {
    if (!window.electronAPI?.selectDirectory) return ''
    return window.electronAPI.selectDirectory(currentPath)
  }

  const joinOutputPath = (root: string, folder: string) => {
    const trimmed = root.trim().replace(/[\\/]+$/, '')
    if (!trimmed) return folder
    const separator = trimmed.includes('\\') ? '\\' : '/'
    return `${trimmed}${separator}${folder}`
  }

  const setOutputRoot = async () => {
    if (!draft) return
    const currentRoot = draft.browser.screenshots_dir || draft.browser.downloads_dir
    const selected = await chooseDirectory(currentRoot)
    if (!selected) return
    updateDraft((current) => ({
      ...current,
      browser: {
        ...current.browser,
        screenshots_dir: joinOutputPath(selected, 'Screenshots'),
        downloads_dir: joinOutputPath(selected, 'Downloads'),
      },
    }))
  }

  const chooseBrowserFolder = async (
    key: 'downloads_dir' | 'screenshots_dir',
  ) => {
    if (!draft) return
    const selected = await chooseDirectory(draft.browser[key])
    if (!selected) return
    updateDraft((current) => ({
      ...current,
      browser: { ...current.browser, [key]: selected },
    }))
  }

  const saveKeys = async () => {
    if (!window.electronAPI) return null
    const writes: Array<[ConnectionSecretId, string]> = [
      ['openai', openaiKey],
      ['google', googleKey],
      ['tavily', tavilyKey],
      ['telegramBot', telegramBotToken],
    ]
    for (const [secret, value] of writes) {
      if (!dirtySecrets.has(secret)) continue
      if (value) await window.electronAPI.setCredential(secret, value)
      else await window.electronAPI.deleteCredential(secret)
    }
    return window.electronAPI.applyStoredCredentials()
  }

  const updateConnectionSecret = (secret: ConnectionSecretId, value: string) => {
    setDirtySecrets((current) => {
      const next = new Set(current)
      next.add(secret)
      return next
    })
    switch (secret) {
      case 'openai':
        setOpenaiKey(value)
        return
      case 'google':
        setGoogleKey(value)
        return
      case 'tavily':
        setTavilyKey(value)
        return
      case 'telegramBot':
        setTelegramBotToken(value)
    }
  }

  const handleSave = async () => {
    if (!draft) return
    if (duplicateMcpServerNames(draft.mcp.servers).length > 0) return
    setSaving(true)
    setConfirmDiscard(false)
    try {
      const browserSecretPayload: {
        openai_api_key?: string
        google_api_key?: string
        tavily_api_key?: string
        telegram_bot_token?: string
      } = {}
      if (!window.electronAPI) {
        if (dirtySecrets.has('openai')) browserSecretPayload.openai_api_key = openaiKey
        if (dirtySecrets.has('google')) browserSecretPayload.google_api_key = googleKey
        if (dirtySecrets.has('tavily')) browserSecretPayload.tavily_api_key = tavilyKey
        if (dirtySecrets.has('telegramBot')) browserSecretPayload.telegram_bot_token = telegramBotToken
      }

      const savedSettings = await updateSettings({
        ...(draft.settings_version ? { expected_settings_version: draft.settings_version } : {}),
        llm: draft.llm,
        speech_to_text: draft.speech_to_text,
        mcp: draft.mcp,
        browser: draft.browser,
        memory: draft.memory,
        tools: draft.tools,
        permissions: draft.permissions,
        sandbox: draft.sandbox,
        identity: draft.identity,
        telegram_allowed_user_ids: telegramAllowedUserIds.trim(),
        telegram_allowed_chat_ids: telegramAllowedChatIds.trim(),
        ...browserSecretPayload,
      })
      const normalizedSavedSettings = normalizeDraft(savedSettings, modelOptions)
      const credentialStatus = await saveKeys()
      setDraft(credentialStatus ? {
        ...normalizedSavedSettings,
        api_keys: {
          ...normalizedSavedSettings.api_keys,
          has_openai_key: credentialStatus.openai,
          has_google_key: credentialStatus.google,
          has_tavily_key: credentialStatus.tavily,
          has_telegram_bot_token: credentialStatus.telegramBot,
        },
      } : normalizedSavedSettings)
      setTelegramAllowedUserIds(normalizedSavedSettings.telegram_allowed_user_ids)
      setTelegramAllowedChatIds(normalizedSavedSettings.telegram_allowed_chat_ids)
      setBaselineSignature(draftSignature(
        normalizedSavedSettings,
        normalizedSavedSettings.telegram_allowed_user_ids,
        normalizedSavedSettings.telegram_allowed_chat_ids,
      ))
      setDirtySecrets(new Set())
      await Promise.all([loadMcpDiagnostics(), loadBrowserDiagnostics(), loadSandboxStatus()])
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  const updatePermissionMode = (mode: AgentSettings['permissions']['mode']) => {
    updateDraft((current) => {
      const customProfile = current.permissions.mode === 'custom'
        ? permissionProfileFromPermissions(current.permissions)
        : current.permissions.custom_profile ?? permissionProfileFromPermissions(current.permissions)
      if (mode === 'custom') {
        return {
          ...current,
          permissions: {
            ...applyPermissionProfile(current.permissions, customProfile),
            mode: 'custom',
          },
        }
      }

      const profile = PERMISSION_PROFILES[mode]
      return {
        ...current,
        permissions: {
          ...current.permissions,
          mode,
          confirmations: { ...profile.confirmations },
          allow_delete: profile.allow_delete,
          dangerous_actions_require_confirm: profile.dangerous_actions_require_confirm,
          allow_screen_fallback: profile.allow_screen_fallback,
          custom_profile: customProfile,
        },
      }
    })
  }

  const updateCustomPermissionDraft = (updater: (current: AgentSettings) => AgentSettings) => {
    updateDraft((current) => {
      const base = current.permissions.mode === 'custom'
        ? current
        : {
            ...current,
            permissions: {
              ...current.permissions,
              confirmations: { ...PERMISSION_PROFILES[current.permissions.mode].confirmations },
              allow_delete: PERMISSION_PROFILES[current.permissions.mode].allow_delete,
              dangerous_actions_require_confirm:
                PERMISSION_PROFILES[current.permissions.mode].dangerous_actions_require_confirm,
              allow_screen_fallback: PERMISSION_PROFILES[current.permissions.mode].allow_screen_fallback,
            },
          }
      const next = updater(base)
      const customProfile = permissionProfileFromPermissions(next.permissions)
      return {
        ...next,
        permissions: {
          ...next.permissions,
          mode: 'custom',
          custom_profile: customProfile,
        },
      }
    })
  }

  const isDirty = useMemo(() => {
    if (!draft) return false
    if (dirtySecrets.size > 0) return true
    if (!baselineSignature) return false
    return draftSignature(draft, telegramAllowedUserIds, telegramAllowedChatIds) !== baselineSignature
  }, [draft, dirtySecrets, baselineSignature, telegramAllowedUserIds, telegramAllowedChatIds])

  const requestClose = () => {
    if (isDirty) {
      setConfirmDiscard(true)
      return
    }
    onClose()
  }

  // Escape closes, but never silently throws away edits.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.stopPropagation()
      requestClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDirty])

  const visibleTabs = useMemo(() => {
    const query = navQuery.trim().toLowerCase()
    if (!query) return null
    return ALL_TABS.filter((tab) => {
      const copy = PAGE_COPY[tab]
      return `${copy.label} ${copy.blurb} ${copy.keywords}`.toLowerCase().includes(query)
    })
  }, [navQuery])

  if (!draft) {
    return (
      <div className="st-overlay fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="settings-shell max-w-md rounded-2xl px-5 py-4">
          {loadError ? (
            <div>
              <p className="st-label" style={{ color: 'var(--st-danger)' }}>Settings failed to load.</p>
              <p className="st-desc mt-1">{loadError}</p>
            </div>
          ) : (
            <p className="st-desc">Loading settings…</p>
          )}
        </div>
      </div>
    )
  }

  const browserSkill = draft.available_skills.find((skill) => skill.name === 'browser-use')
  const displayedPermissions = draft.permissions.mode === 'custom'
    ? draft.permissions
    : {
        ...draft.permissions,
        ...PERMISSION_PROFILES[draft.permissions.mode],
      }
  const skillGroups = {
    recommended: draft.available_skills.filter((skill) => !skill.hidden && skill.tier === 'recommended'),
    optional: draft.available_skills.filter((skill) => !skill.hidden && skill.tier === 'optional'),
  }
  const browserSkillEnabled = !!draft.tools.skills['browser-use'] && (browserSkill?.available ?? true)
  const mcpFeatureEnabled = !!draft.mcp.enabled
  const mcpFeatureDiagnostics = Object.values(mcpDiagnostics)
  const mcpFeatureAvailable = mcpFeatureDiagnostics[0]?.feature_available ?? true
  const mcpFeatureUnavailableReason = mcpFeatureDiagnostics[0]?.feature_unavailable_reason ?? ''
  const allowedDomainsInput = draft.browser.allowed_domains.join(', ')
  const duplicateMcpNames = duplicateMcpServerNames(draft.mcp.servers)
  const connectionSecretValues: ConnectionSecretValues = {
    openai: openaiKey,
    google: googleKey,
    tavily: tavilyKey,
    telegramBot: telegramBotToken,
  }
  const connectionSecretStatuses: ConnectionSecretStatuses = {
    openai: draft.api_keys.has_openai_key,
    google: draft.api_keys.has_google_key,
    tavily: draft.api_keys.has_tavily_key,
    telegramBot: Boolean(draft.api_keys.has_telegram_bot_token),
  }
  const telegramAllowlistConfigured = Boolean(telegramAllowedUserIds.trim() || telegramAllowedChatIds.trim())
  const customInstructionsDirty = customInstructions !== customInstructionsSavedContent
  const speechToTextIsCloud = draft.speech_to_text.engine === 'cloud'
  const speechToTextReady = speechToTextIsCloud ? Boolean(speechToTextStatus?.cloud_configured) : Boolean(speechToTextStatus?.downloaded)
  const speechToTextPanelStatus = speechToTextIsCloud
    ? (speechToTextStatus?.cloud_configured ? 'Uses your saved Google API key.' : 'Save a Google API key in Connections.')
    : deletingSpeechToText
      ? 'Deleting local speech model…'
      : offloadingSpeechToText
        ? 'Offloading speech model…'
        : speechToTextStatus?.loaded
          ? 'Model is loaded in memory.'
          : speechToTextStatus?.downloaded
            ? 'Local transcription is ready.'
            : downloadingSpeechToText
              ? 'Downloading Whisper base…'
              : 'Download once before voice input.'

  const getSkillHealth = (skill: AgentSettings['available_skills'][number]) => {
    if (!skill.available || skill.load_error) {
      return `Unavailable: ${formatUnavailableReason(skill.unavailable_reason, skill.load_error)}`
    }
    if (skill.name === 'browser-use') {
      if (!browserSkillEnabled) return draft.tools.skills['browser-use'] ? 'Unavailable' : 'Off'
      if (!browserDiagnostics) return 'Checking'
      if (browserDiagnostics.session_active && browserDiagnostics.current_mode) {
        return browserDiagnostics.current_mode === 'managed' ? 'Managed active' : 'System active'
      }
      if (browserDiagnostics.last_error) return 'Degraded'
      return 'Ready'
    }
    return skill.recommended ? 'On by default' : 'Optional'
  }

  const skillHealthTone = (skill: AgentSettings['available_skills'][number]) => {
    const health = getSkillHealth(skill)
    if (health.startsWith('Unavailable') || health === 'Degraded') return 'warn' as const
    if (['Ready', 'On by default', 'Managed active', 'System active'].includes(health)) return 'ok' as const
    return 'neutral' as const
  }

  const footerMessage = duplicateMcpNames.length > 0
    ? { icon: AlertCircle, tone: 'danger' as const, text: FOOTER_COPY.duplicateMcp }
    : saved
      ? { icon: CheckCircle2, tone: 'ok' as const, text: FOOTER_COPY.saved }
      : isDirty
        ? { icon: AlertCircle, tone: 'warn' as const, text: FOOTER_COPY.dirty }
        : { icon: CheckCircle2, tone: 'neutral' as const, text: FOOTER_COPY.clean }

  return (
    <div
      className="st-overlay fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={(e) => e.target === e.currentTarget && requestClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        className="settings-shell flex h-[min(92vh,940px)] w-full max-w-6xl overflow-hidden rounded-2xl"
      >
        {/* ------------------------------------------------------ navigation */}
        <aside className="st-nav hidden h-full w-56 shrink-0 flex-col p-3 md:flex">
          <div className="mb-3 flex items-center justify-between gap-2 px-1">
            <h2 className="st-title">Settings</h2>
            <button
              type="button"
              onClick={requestClose}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label="Close settings"
            >
              <X size={15} />
            </button>
          </div>

          <div className="relative mb-3">
            <Search
              size={13}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2"
              style={{ color: 'var(--st-text-faint)' }}
            />
            <input
              value={navQuery}
              onChange={(e) => setNavQuery(e.target.value)}
              placeholder="Search settings"
              aria-label="Search settings"
              className="st-input st-search"
            />
          </div>

          <nav className="st-scroll-hidden min-h-0 flex-1 overflow-y-auto">
            {visibleTabs ? (
              visibleTabs.length === 0 ? (
                <p className="st-hint px-1 py-2">No settings match “{navQuery}”.</p>
              ) : (
                <div className="space-y-0.5">
                  {visibleTabs.map((tab) => (
                    <NavItem
                      key={tab}
                      tab={tab}
                      active={activeTab === tab}
                      onSelect={() => setActiveTab(tab)}
                    />
                  ))}
                </div>
              )
            ) : (
              NAV_GROUPS.map((group) => (
                <div key={group.id} className="mb-3">
                  <p className="st-group-label mb-1.5 px-1">{group.label}</p>
                  <div className="space-y-0.5">
                    {group.items.map((tab) => (
                      <NavItem
                        key={tab}
                        tab={tab}
                        active={activeTab === tab}
                        onSelect={() => setActiveTab(tab)}
                      />
                    ))}
                  </div>
                </div>
              ))
            )}
          </nav>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          {/* ------------------------------------------------ compact header */}
          <header className="st-divider-b flex items-center justify-between gap-3 px-5 py-3 md:hidden">
            <h2 className="st-title">Settings</h2>
            <button
              type="button"
              onClick={requestClose}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label="Close settings"
            >
              <X size={15} />
            </button>
          </header>

          <div className="st-divider-b px-4 py-3 md:hidden">
            <Dropdown<SettingsTab>
              ariaLabel="Settings section"
              value={activeTab}
              onChange={setActiveTab}
              options={ALL_TABS.map((tab) => ({ value: tab, label: PAGE_COPY[tab].label }))}
            />
          </div>

          {/* ---------------------------------------------------- page body */}
          <div ref={scrollRef} className="st-scroll min-h-0 flex-1 overflow-y-auto px-6 py-6">
            <div className="mx-auto w-full max-w-[46rem]">
              {activeTab === 'model' && (
                <>
                  <PageHeader title={PAGE_COPY.model.label} description={PAGE_COPY.model.blurb} />
                  <div className="space-y-4">
                    <SettingsCard
                      title="Which model to use"
                      footnote="Each provider needs its own API key saved under Connections."
                    >
                      <SettingRow label="Provider" description={MODEL_COPY.provider}>
                        <Dropdown<AgentSettings['llm']['provider']>
                          ariaLabel="Provider"
                          value={draft.llm.provider}
                          options={providerOptions(modelOptions).map((p) => ({
                            value: p.id,
                            label: providerLabel(p.id, modelOptions),
                          }))}
                          onChange={(provider) => {
                            const modelName = modelsForProvider(provider, modelOptions)[0]
                            const reasoningOptions = reasoningEffortsForProvider(provider, modelName)
                            updateDraft((current) => ({
                              ...current,
                              llm: {
                                ...current.llm,
                                provider,
                                model_name: modelName,
                                reasoning_effort: reasoningOptions.includes(current.llm.reasoning_effort)
                                  ? current.llm.reasoning_effort
                                  : reasoningOptions[0],
                              },
                            }))
                          }}
                        />
                      </SettingRow>
                      <SettingRow label="Model" description={MODEL_COPY.model}>
                        <Dropdown<string>
                          ariaLabel="Model"
                          value={draft.llm.model_name}
                          options={modelsForProvider(draft.llm.provider, modelOptions).map((m) => ({ value: m, label: m }))}
                          onChange={(model_name) => updateDraft((current) => {
                            const reasoningOptions = reasoningEffortsForProvider(current.llm.provider, model_name)
                            return {
                              ...current,
                              llm: {
                                ...current.llm,
                                model_name,
                                reasoning_effort: reasoningOptions.includes(current.llm.reasoning_effort)
                                  ? current.llm.reasoning_effort
                                  : reasoningOptions[0],
                              },
                            }
                          })}
                        />
                      </SettingRow>
                      <SettingRow label="Reasoning effort" description={MODEL_COPY.reasoningEffort}>
                        <Dropdown<AgentSettings['llm']['reasoning_effort']>
                          ariaLabel="Reasoning effort"
                          value={draft.llm.reasoning_effort}
                          options={reasoningEffortsForProvider(draft.llm.provider, draft.llm.model_name).map((e) => ({
                            value: e,
                            label: formatReasoningEffort(e),
                          }))}
                          onChange={(reasoning_effort) => updateDraft((current) => ({
                            ...current,
                            llm: { ...current.llm, reasoning_effort },
                          }))}
                        />
                      </SettingRow>
                    </SettingsCard>

                    <SettingsCard title="Reading images and screenshots">
                      <div className="px-4 py-4">
                        <Note tone="info">{MODEL_COPY.vision}</Note>
                      </div>
                    </SettingsCard>

                    <SettingsCard
                      title="Voice input"
                      description={MODEL_COPY.speechLocalIntro}
                      action={
                        <Badge tone={speechToTextReady ? 'ok' : 'warn'}>
                          {speechToTextIsCloud
                            ? (speechToTextReady ? 'Cloud ready' : 'Key needed')
                            : (speechToTextReady ? 'Ready' : 'Download needed')}
                        </Badge>
                      }
                    >
                      <SettingRow label="Transcription engine" description={MODEL_COPY.speechEngine}>
                        <Dropdown<AgentSettings['speech_to_text']['engine']>
                          ariaLabel="Transcription engine"
                          value={draft.speech_to_text.engine}
                          options={[
                            { value: 'local', label: 'Local (offline)' },
                            { value: 'cloud', label: 'Cloud (Google)' },
                          ]}
                          onChange={(engine) => updateDraft((current) => ({
                            ...current,
                            speech_to_text: { ...current.speech_to_text, engine },
                          }))}
                        />
                      </SettingRow>
                      <div className="st-row">
                        <div className="min-w-0">
                          <p className="st-label flex items-center gap-2">
                            <Mic size={13} style={{ color: 'var(--st-accent-text)' }} />
                            {speechToTextIsCloud
                              ? draft.speech_to_text.cloud_model
                              : (speechToTextStatus ? `${speechToTextStatus.model_label} (${speechToTextStatus.model_size})` : 'Whisper base (~142 MB)')}
                          </p>
                          <p className="st-desc mt-1">{speechToTextPanelStatus}</p>
                        </div>
                        <div className="st-row-control-auto flex items-center gap-1.5">
                          {speechToTextStatus?.loaded && <Badge tone="ok">In memory</Badge>}
                          <button
                            type="button"
                            onClick={handleDownloadSpeechToText}
                            disabled={speechToTextIsCloud || downloadingSpeechToText || speechToTextStatus?.downloaded}
                            aria-label="Download local speech model"
                            title={speechToTextStatus?.downloaded ? 'Already downloaded' : 'Download the model (about 142 MB)'}
                            className="st-btn st-btn-secondary st-btn-icon"
                          >
                            {downloadingSpeechToText ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}
                          </button>
                          <button
                            type="button"
                            onClick={handleOffloadSpeechToText}
                            disabled={speechToTextIsCloud || offloadingSpeechToText || !speechToTextStatus?.loaded}
                            aria-label="Offload local speech model"
                            title="Free the memory it is using, keeping the download"
                            className="st-btn st-btn-secondary st-btn-icon"
                          >
                            {offloadingSpeechToText ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
                          </button>
                          <button
                            type="button"
                            onClick={handleDeleteSpeechToText}
                            disabled={speechToTextIsCloud || deletingSpeechToText || !speechToTextStatus?.downloaded}
                            aria-label="Delete local speech model"
                            title="Delete the downloaded model from disk"
                            className="st-btn st-btn-danger st-btn-icon"
                          >
                            {deletingSpeechToText ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
                          </button>
                        </div>
                      </div>
                      {speechToTextError && (
                        <div className="px-4 pb-4">
                          <Note tone="danger">{speechToTextError}</Note>
                        </div>
                      )}
                    </SettingsCard>

                    <SettingsCard title="Limits for a single request" description={MODEL_COPY.runtimeLimits}>
                      <NumberRow
                        label="Maximum tool calls"
                        description={MODEL_COPY.maxIterations}
                        ariaLabel="Tool iteration limit"
                        unit="calls"
                        min={1}
                        max={500}
                        value={draft.llm.max_iterations_per_turn}
                        onChange={(value) => updateLlmNumber('max_iterations_per_turn', value, 1, 500)}
                      />
                      <NumberRow
                        label="Time budget per message"
                        description={MODEL_COPY.maxTurnSeconds}
                        ariaLabel="Turn timeout seconds"
                        unit="seconds"
                        min={30}
                        max={14400}
                        value={draft.llm.max_turn_seconds}
                        onChange={(value) => updateLlmNumber('max_turn_seconds', value, 30, 14400)}
                      />
                      <NumberRow
                        label="Timeout per model reply"
                        description={MODEL_COPY.maxLlmCallSeconds}
                        ariaLabel="LLM call timeout seconds"
                        unit="seconds"
                        min={30}
                        max={1800}
                        value={draft.llm.max_llm_call_seconds}
                        onChange={(value) => updateLlmNumber('max_llm_call_seconds', value, 30, 1800)}
                      />
                    </SettingsCard>
                  </div>
                </>
              )}

              {activeTab === 'apiKeys' && (
                <>
                  <PageHeader title={PAGE_COPY.apiKeys.label} description={PAGE_COPY.apiKeys.blurb} />
                  <div className="space-y-4">
                    <SettingsCard>
                      <div className="p-4">
                        <ConnectionPortalPanel
                          selectedPortal={activeConnectionPortal}
                          values={connectionSecretValues}
                          statuses={connectionSecretStatuses}
                          showSecrets={showKey}
                          onPortalChange={setActiveConnectionPortal}
                          onSecretChange={updateConnectionSecret}
                          onToggleSecrets={() => setShowKey((value) => !value)}
                        />
                      </div>
                    </SettingsCard>

                    {activeConnectionPortal === 'telegram' && (
                      <SettingsCard
                        title="Who may talk to the agent on Telegram"
                        description="Only the accounts and groups listed here can send the agent messages. Leave both empty and nobody gets through."
                        action={
                          <Badge tone={telegramAllowlistConfigured ? 'ok' : 'warn'}>
                            {telegramAllowlistConfigured ? 'Configured' : 'Required'}
                          </Badge>
                        }
                      >
                        <StackedRow
                          label="Allowed user IDs"
                          description="Your Telegram account's numeric ID. Separate several with commas."
                        >
                          <input
                            value={telegramAllowedUserIds}
                            aria-label="Allowed user IDs"
                            onChange={(e) => setTelegramAllowedUserIds(e.target.value)}
                            placeholder="123456789, 987654321"
                            className="st-input"
                          />
                        </StackedRow>
                        <StackedRow
                          label="Allowed chat IDs"
                          description="Group chat IDs, usually a negative number starting with -100."
                        >
                          <input
                            value={telegramAllowedChatIds}
                            aria-label="Allowed chat IDs"
                            onChange={(e) => setTelegramAllowedChatIds(e.target.value)}
                            placeholder="-1001234567890"
                            className="st-input"
                          />
                        </StackedRow>
                      </SettingsCard>
                    )}
                  </div>
                </>
              )}

              {activeTab === 'identity' && (
                <>
                  <PageHeader title={PAGE_COPY.identity.label} description={PAGE_COPY.identity.blurb} />
                  <div className="space-y-4">
                    <SettingsCard title="Names">
                      <SettingRow label="Agent nickname" description={IDENTITY_COPY.agentName}>
                        <input
                          value={draft.identity.agent_name}
                          aria-label="Agent nickname"
                          maxLength={80}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, agent_name: e.target.value },
                          }))}
                          placeholder={DEFAULT_AGENT_NAME}
                          className="st-input"
                        />
                      </SettingRow>
                      <SettingRow label="Call me" description={IDENTITY_COPY.userName}>
                        <input
                          value={draft.identity.user_name}
                          aria-label="Call me"
                          maxLength={80}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, user_name: e.target.value },
                          }))}
                          placeholder="Your preferred name"
                          className="st-input"
                        />
                      </SettingRow>
                    </SettingsCard>

                    <SettingsCard title="How the agent should treat you">
                      <StackedRow label="User identity" description={IDENTITY_COPY.userIdentity}>
                        <textarea
                          value={draft.identity.user_identity}
                          aria-label="User identity"
                          maxLength={1000}
                          rows={4}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, user_identity: e.target.value },
                          }))}
                          placeholder="Auditor at a mid-size firm. Windows only. Prefers Excel over CSV."
                          className="st-input resize-none leading-relaxed"
                        />
                      </StackedRow>
                      <StackedRow label="Communication style" description={IDENTITY_COPY.communicationStyle}>
                        <textarea
                          value={draft.identity.communication_style}
                          aria-label="Communication style"
                          maxLength={1000}
                          rows={4}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, communication_style: e.target.value },
                          }))}
                          placeholder="Short answers. Lead with the conclusion. No filler."
                          className="st-input resize-none leading-relaxed"
                        />
                      </StackedRow>
                    </SettingsCard>

                    <SettingsCard
                      title="Custom instructions"
                      description={IDENTITY_COPY.customInstructions}
                      action={
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={handleResetCustomInstructions}
                            disabled={resettingCustomInstructions || savingCustomInstructions}
                            aria-label="Restore default custom instructions"
                            className="st-btn st-btn-ghost"
                          >
                            {resettingCustomInstructions ? <Loader2 size={13} className="animate-spin" /> : <RotateCcw size={13} />}
                            Restore default
                          </button>
                          <button
                            type="button"
                            onClick={handleSaveCustomInstructions}
                            disabled={!customInstructionsDirty || savingCustomInstructions || resettingCustomInstructions}
                            aria-label="Apply custom instructions"
                            className="st-btn st-btn-primary"
                          >
                            {savingCustomInstructions ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}
                            {customInstructionsDirty ? 'Apply' : 'Applied'}
                          </button>
                        </div>
                      }
                      footnote={
                        <>
                          <span className="block">{IDENTITY_COPY.customInstructionsLimit}</span>
                          <span className="mt-1 block break-all">Stored at {customInstructionsPath || 'AGENTS.md'}</span>
                        </>
                      }
                    >
                      <div className="p-4">
                        <textarea
                          aria-label="Custom instructions"
                          value={customInstructions}
                          maxLength={20000}
                          rows={10}
                          onChange={(e) => setCustomInstructions(e.target.value)}
                          placeholder="# AGENTS.md"
                          className="st-input resize-y font-mono text-xs leading-relaxed"
                        />
                        {customInstructionsError && (
                          <div className="mt-3">
                            <Note tone="danger">{customInstructionsError}</Note>
                          </div>
                        )}
                      </div>
                    </SettingsCard>

                    <SettingsCard title="What the agent will use">
                      <div className="grid gap-3 p-4 sm:grid-cols-2">
                        <ReadOnlyValue label="Agent name" value={resolveAgentName(draft.identity.agent_name)} />
                        <ReadOnlyValue label="Your name" value={draft.identity.user_name.trim()} />
                        <ReadOnlyValue label="Instructions file" value={customInstructionsPath} />
                        <ReadOnlyValue
                          label="Style"
                          value={draft.identity.communication_style.trim() || 'Default'}
                        />
                      </div>
                    </SettingsCard>
                  </div>
                </>
              )}

              {activeTab === 'skills' && (
                <>
                  <PageHeader title={PAGE_COPY.skills.label} description={PAGE_COPY.skills.blurb} />
                  <div className="space-y-4">
                    <Note tone="info">{SKILLS_COPY.intro}</Note>
                    {(['recommended', 'optional'] as const).map((tier) => (
                      <SettingsCard
                        key={tier}
                        title={SKILL_GROUP_COPY[tier].title}
                        description={SKILL_GROUP_COPY[tier].description}
                        action={<Badge>{skillGroups[tier].length}</Badge>}
                      >
                        {skillGroups[tier].length === 0 ? (
                          <p className="st-hint p-4">Nothing here.</p>
                        ) : (
                          skillGroups[tier].map((skill) => {
                            const effectiveEnabled = skill.available && !!draft.tools.skills[skill.name]
                            const isExpanded = expandedSkill === skill.name
                            return (
                              <div key={skill.name} className="st-row-stacked">
                                <div className="flex items-start justify-between gap-4">
                                  <button
                                    type="button"
                                    onClick={() => setExpandedSkill(isExpanded ? null : skill.name)}
                                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                  >
                                    <span className="st-label truncate">{skill.display_name}</span>
                                    <Badge tone={skillHealthTone(skill)}>{getSkillHealth(skill)}</Badge>
                                    <ChevronDown
                                      size={12}
                                      className={`shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                                      style={{ color: 'var(--st-text-faint)' }}
                                    />
                                  </button>
                                  <Switch
                                    label={skill.display_name}
                                    checked={effectiveEnabled}
                                    disabled={!skill.available || skill.always}
                                    onChange={(checked) => updateDraft((current) => ({
                                      ...current,
                                      tools: {
                                        ...current.tools,
                                        skills: { ...current.tools.skills, [skill.name]: checked },
                                      },
                                    }))}
                                  />
                                </div>
                                <p className="st-desc mt-1.5">
                                  {skill.summary || skill.description || 'No description available.'}
                                </p>
                                {isExpanded && (
                                  <div className="mt-2 space-y-2">
                                    {skill.description && <p className="st-hint">{skill.description}</p>}
                                    {(!skill.available || skill.load_error) && (
                                      <Note tone="warn">
                                        {SKILLS_COPY.unavailable}{' '}
                                        {formatUnavailableReason(skill.unavailable_reason, skill.load_error)}
                                      </Note>
                                    )}
                                  </div>
                                )}
                              </div>
                            )
                          })
                        )}
                      </SettingsCard>
                    ))}
                  </div>
                </>
              )}

              {activeTab === 'memory' && (
                <>
                  <PageHeader title={PAGE_COPY.memory.label} description={PAGE_COPY.memory.blurb} />
                  <Suspense fallback={null}>
                    <MemorySettingsPanel
                      draft={draft}
                      updateDraft={updateDraft}
                      refreshKey={memoryRefreshKey}
                    />
                  </Suspense>
                </>
              )}

              {activeTab === 'browser' && (
                <>
                  <PageHeader
                    title={PAGE_COPY.browser.label}
                    description={PAGE_COPY.browser.blurb}
                    action={
                      <button
                        type="button"
                        onClick={handleResetBrowserSession}
                        disabled={resettingBrowserSession || !browserSkillEnabled}
                        title={BROWSER_COPY.resetSession}
                        className="st-btn st-btn-secondary"
                      >
                        <RotateCcw size={13} />
                        {resettingBrowserSession ? 'Resetting' : 'Reset session'}
                      </button>
                    }
                  />
                  <div className="space-y-4">
                    {browserSkill?.available === false && (
                      <Note tone="warn" icon={AlertCircle}>
                        The browser-use package is not installed in the backend Python environment, so browser
                        automation is unavailable.
                      </Note>
                    )}

                    <SettingsCard title="Status">
                      <div className="grid grid-cols-3">
                        <StatusCell label="Skill" value={browserSkillEnabled ? 'On' : 'Off'} />
                        <StatusCell
                          label="Session"
                          value={
                            browserSkill?.available === false
                              ? 'Unavailable'
                              : browserDiagnostics?.session_active && browserDiagnostics.current_mode
                                ? `${browserDiagnostics.current_mode} active`
                                : browserDiagnostics?.last_error
                                  ? 'Degraded'
                                  : browserSkillEnabled ? 'Idle' : 'Off'
                          }
                        />
                        <StatusCell label="Open tabs" value={String(browserDiagnostics?.tab_count ?? 0)} />
                      </div>
                    </SettingsCard>

                    <SettingsCard title="Which browser to drive">
                      <SettingRow label="Launch mode" description={BROWSER_COPY.mode}>
                        <Dropdown<AgentSettings['browser']['mode']>
                          ariaLabel="Launch mode"
                          value={draft.browser.mode}
                          options={[
                            { value: 'auto', label: 'Managed, then yours' },
                            { value: 'managed', label: 'Managed only' },
                            { value: 'system', label: 'Your Chrome only' },
                          ]}
                          onChange={(mode) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, mode },
                          }))}
                        />
                      </SettingRow>
                      <SwitchRow
                        label="Fall back to your Chrome"
                        description={BROWSER_COPY.systemFallback}
                        checked={draft.browser.enable_system_fallback}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, enable_system_fallback: checked },
                        }))}
                      />
                      <SwitchRow
                        label="Run invisibly"
                        description={BROWSER_COPY.headless}
                        checked={draft.browser.headless}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, headless: checked },
                        }))}
                      />
                      <SwitchRow
                        label="Keep the session open"
                        description={BROWSER_COPY.keepAlive}
                        checked={draft.browser.keep_alive}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, keep_alive: checked },
                        }))}
                      />
                    </SettingsCard>

                    <SettingsCard title="Connecting to your own Chrome">
                      <SettingRow label="Connection strategy" description={BROWSER_COPY.systemConnection}>
                        <Dropdown<'auto' | 'attach'>
                          ariaLabel="Connection strategy"
                          value={draft.browser.system_connection_strategy === 'launch' ? 'auto' : draft.browser.system_connection_strategy}
                          options={[
                            { value: 'auto', label: 'Attach, else launch' },
                            { value: 'attach', label: 'Attach only' },
                          ]}
                          onChange={(system_connection_strategy) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, system_connection_strategy },
                          }))}
                        />
                      </SettingRow>
                      <SettingRow label="Debugging address" description={BROWSER_COPY.cdpUrl} wide>
                        <input
                          value={draft.browser.system_cdp_url}
                          aria-label="Debugging address"
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, system_cdp_url: e.target.value },
                          }))}
                          placeholder="http://127.0.0.1:9222"
                          className="st-input font-mono text-xs"
                        />
                      </SettingRow>
                      <SettingRow label="Chrome profile" description={BROWSER_COPY.chromeProfile}>
                        <Dropdown
                          ariaLabel="Chrome profile"
                          value={draft.browser.system_profile_directory || ''}
                          options={[
                            { value: '', label: 'Detect automatically' },
                            ...(browserDiagnostics?.available_system_profiles ?? []).map((p) => ({
                              value: p.directory,
                              label: `${p.name} (${p.directory})`,
                            })),
                          ]}
                          onChange={(system_profile_directory) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, system_profile_directory },
                          }))}
                        />
                      </SettingRow>
                      <SettingRow label="Allowed domains" description={BROWSER_COPY.allowedDomains} wide>
                        <input
                          value={allowedDomainsInput}
                          aria-label="Allowed domains"
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            browser: {
                              ...current.browser,
                              allowed_domains: e.target.value.split(',').map((item) => item.trim()).filter(Boolean),
                            },
                          }))}
                          placeholder="example.com, docs.example.com"
                          className="st-input"
                        />
                      </SettingRow>
                    </SettingsCard>

                    <SettingsCard
                      title="Where files are saved"
                      description={BROWSER_COPY.outputWorkspace}
                      action={window.electronAPI?.selectDirectory ? (
                        <button type="button" onClick={setOutputRoot} className="st-btn st-btn-secondary">
                          <Folder size={13} />
                          Choose folder
                        </button>
                      ) : undefined}
                    >
                      <StackedRow label="Screenshots">
                        <OutputFolderInput
                          label="Screenshots folder"
                          value={draft.browser.screenshots_dir}
                          onChange={(screenshots_dir) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, screenshots_dir },
                          }))}
                          onBrowse={window.electronAPI?.selectDirectory ? () => chooseBrowserFolder('screenshots_dir') : undefined}
                        />
                      </StackedRow>
                      <StackedRow label="Downloads">
                        <OutputFolderInput
                          label="Downloads folder"
                          value={draft.browser.downloads_dir}
                          onChange={(downloads_dir) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, downloads_dir },
                          }))}
                          onBrowse={window.electronAPI?.selectDirectory ? () => chooseBrowserFolder('downloads_dir') : undefined}
                        />
                      </StackedRow>
                      <div className="st-row-stacked grid gap-3 sm:grid-cols-2">
                        <ReadOnlyValue label={`Managed profile — ${BROWSER_COPY.managedProfile}`} value={draft.browser.managed_profile_dir} />
                        <ReadOnlyValue label={`Traces — ${BROWSER_COPY.traces}`} value={draft.browser.traces_dir} />
                      </div>
                    </SettingsCard>

                    <Disclosure
                      open={browserAdvancedOpen}
                      onToggle={() => setBrowserAdvancedOpen((v) => !v)}
                      title="Advanced page reading"
                      description={BROWSER_COPY.advanced}
                    >
                      <SettingRow label="Page analysis engine" description={BROWSER_COPY.domEngine}>
                        <Dropdown<AgentSettings['browser']['dom_inspection_engine']>
                          ariaLabel="Page analysis engine"
                          value={draft.browser.dom_inspection_engine}
                          options={[
                            { value: 'auto', label: 'Automatic' },
                            { value: 'enhanced', label: 'Enhanced' },
                            { value: 'legacy', label: 'Legacy' },
                          ]}
                          onChange={(dom_inspection_engine) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, dom_inspection_engine },
                          }))}
                        />
                      </SettingRow>
                      <SwitchRow
                        label="Skip covered elements"
                        description={BROWSER_COPY.paintOrder}
                        checked={draft.browser.paint_order_filtering}
                        onChange={(paint_order_filtering) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, paint_order_filtering },
                        }))}
                      />
                      <SwitchRow
                        label="Read third-party frames"
                        description={BROWSER_COPY.crossOrigin}
                        checked={draft.browser.cross_origin_iframes}
                        onChange={(cross_origin_iframes) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, cross_origin_iframes },
                        }))}
                      />
                      <NumberRow
                        label="Frames per page"
                        description={BROWSER_COPY.maxIframes}
                        ariaLabel="Maximum frames per page"
                        min={0}
                        max={20}
                        value={draft.browser.max_iframes}
                        onChange={(value) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, max_iframes: Number(value) },
                        }))}
                      />
                      <NumberRow
                        label="Frame nesting depth"
                        description={BROWSER_COPY.frameDepth}
                        ariaLabel="Maximum frame depth"
                        min={0}
                        max={5}
                        value={draft.browser.max_iframe_depth}
                        onChange={(value) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, max_iframe_depth: Number(value) },
                        }))}
                      />
                    </Disclosure>

                    <Disclosure
                      open={diagOpen}
                      onToggle={() => setDiagOpen((v) => !v)}
                      title="Diagnostics"
                      description="Raw connection details, useful when reporting a problem."
                    >
                      <BrowserDiagnosticsCard diagnostics={browserDiagnostics} draft={draft} />
                    </Disclosure>
                  </div>
                </>
              )}

              {activeTab === 'mcp' && (
                <>
                  <PageHeader
                    title={PAGE_COPY.mcp.label}
                    description={PAGE_COPY.mcp.blurb}
                    action={
                      <div className="flex flex-wrap items-center gap-2">
                        <Dropdown<MCPServerTemplateKey>
                          ariaLabel="Server template"
                          value={mcpTemplate}
                          options={(Object.entries(MCP_SERVER_TEMPLATES) as Array<[MCPServerTemplateKey, { label: string; build: () => unknown }]>).map(
                            ([key, template]) => ({ value: key, label: template.label }),
                          )}
                          onChange={setMcpTemplate}
                          size="sm"
                          className="min-w-44"
                        />
                        <button
                          type="button"
                          onClick={() => updateDraft((current) => ({
                            ...current,
                            mcp: {
                              ...current.mcp,
                              servers: [...current.mcp.servers, MCP_SERVER_TEMPLATES[mcpTemplate].build()],
                            },
                          }))}
                          className="st-btn st-btn-primary"
                        >
                          <Plus size={13} />
                          Add server
                        </button>
                      </div>
                    }
                  />
                  <div className="space-y-4">
                    <SettingsCard>
                      <SwitchRow
                        label="MCP bridge"
                        description={MCP_COPY.bridge}
                        checked={mcpFeatureEnabled}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          mcp: { ...current.mcp, enabled: checked },
                        }))}
                      />
                    </SettingsCard>

                    {!mcpFeatureAvailable && (
                      <Note tone="warn" icon={AlertCircle}>
                        The MCP bridge is built in, but the backend Python environment is missing the mcp package
                        {mcpFeatureUnavailableReason ? ` (${mcpFeatureUnavailableReason})` : ''}.
                      </Note>
                    )}
                    {duplicateMcpNames.length > 0 && (
                      <Note tone="danger" icon={AlertCircle}>
                        MCP server names must be unique. Duplicated: {duplicateMcpNames.join(', ')}
                      </Note>
                    )}

                    {draft.mcp.servers.length === 0 ? (
                      <SettingsCard>
                        <div className="p-8 text-center">
                          <p className="st-label">No MCP servers yet</p>
                          <p className="st-desc mx-auto mt-1.5">{MCP_COPY.intro}</p>
                        </div>
                      </SettingsCard>
                    ) : (
                      draft.mcp.servers.map((server, index) => {
                        const diagnostic = server.name ? mcpDiagnostics[server.name] : undefined
                        const statusText = !mcpFeatureEnabled
                          ? 'MCP bridge is off'
                          : diagnostic?.feature_available === false
                            ? 'Backend dependency missing'
                          : diagnostic
                            ? diagnostic.connected
                              ? `Connected — ${diagnostic.tool_count} tools`
                              : diagnostic.state === 'unhealthy'
                                ? `Unhealthy — ${diagnostic.unhealthy_reason || diagnostic.last_error || 'liveness check failed'}`
                                : diagnostic.last_error || 'Not connected'
                            : 'Save or reconnect to check'

                        return (
                          <McpServerCard
                            key={`mcp-server-${index}`}
                            server={server}
                            index={index}
                            diagnostic={diagnostic}
                            statusText={statusText}
                            bridgeEnabled={mcpFeatureEnabled && (diagnostic?.feature_available ?? mcpFeatureAvailable)}
                            reconnectingServer={reconnectingServer}
                            advancedOpen={!!openMcpAdvanced[index]}
                            onToggleAdvanced={() => setOpenMcpAdvanced((current) => ({ ...current, [index]: !current[index] }))}
                            onReconnect={() => handleReconnectServer(server.name)}
                            onRemove={() => updateDraft((current) => ({
                              ...current,
                              mcp: {
                                ...current.mcp,
                                servers: current.mcp.servers.filter((_, serverIndex) => serverIndex !== index),
                              },
                            }))}
                            onUpdate={(updater) => updateMcpServer(index, updater)}
                          />
                        )
                      })
                    )}

                    <Note tone="warn">{MCP_COPY.plaintextWarning}</Note>
                  </div>
                </>
              )}

              {activeTab === 'observability' && (
                <>
                  <PageHeader title={PAGE_COPY.observability.label} description={PAGE_COPY.observability.blurb} />
                  <Suspense fallback={null}>
                    <ObservabilityPanel refreshKey={observabilityRefreshKey} />
                  </Suspense>
                </>
              )}

              {activeTab === 'permissions' && (
                <>
                  <PageHeader title={PAGE_COPY.permissions.label} description={PAGE_COPY.permissions.blurb} />
                  <div className="space-y-4">
                    <SettingsCard title="Approval profile" description="Start here. The switches below follow whichever profile you pick.">
                      <div className="grid gap-2 p-4 sm:grid-cols-3">
                        {(['default', 'full_access', 'custom'] as const).map((mode) => (
                          <ChoiceCard
                            key={mode}
                            ariaLabel={PERMISSION_MODE_LABEL[mode]}
                            title={PERMISSION_MODE_COPY[mode].title}
                            summary={PERMISSION_MODE_COPY[mode].summary}
                            hint={PERMISSION_MODE_COPY[mode].recommendation}
                            tone={mode === 'default' ? 'ok' : mode === 'full_access' ? 'warn' : 'neutral'}
                            selected={draft.permissions.mode === mode}
                            onSelect={() => updatePermissionMode(mode)}
                          />
                        ))}
                      </div>
                      {draft.permissions.mode === 'full_access' && (
                        <div className="px-4 pb-4">
                          <Note tone="warn" icon={AlertCircle}>
                            The agent will edit and create files without asking. Deleting still stays off unless you
                            turn it on below.
                          </Note>
                        </div>
                      )}
                    </SettingsCard>

                    <SettingsCard
                      title="Stop and ask me before…"
                      description="Turning any of these off switches the profile to Custom."
                    >
                      {(Object.entries(displayedPermissions.confirmations) as Array<[keyof AgentSettings['permissions']['confirmations'], boolean]>).map(([key, value]) => {
                        const copy = CONFIRMATION_COPY[key] ?? { label: key, description: '' }
                        return (
                          <SwitchRow
                            key={key}
                            label={copy.label}
                            description={copy.description}
                            checked={value}
                            onChange={(checked) => updateCustomPermissionDraft((current) => ({
                              ...current,
                              permissions: {
                                ...current.permissions,
                                confirmations: { ...current.permissions.confirmations, [key]: checked },
                              },
                            }))}
                          />
                        )
                      })}
                    </SettingsCard>

                    <SettingsCard title="High-risk actions">
                      <SwitchRow
                        label="Allow delete actions"
                        description={RISK_COPY.allowDelete.description}
                        checked={displayedPermissions.allow_delete}
                        onChange={(checked) => updateCustomPermissionDraft((current) => ({
                          ...current,
                          permissions: { ...current.permissions, allow_delete: checked },
                        }))}
                      />
                      <SwitchRow
                        label="Dangerous actions require confirm"
                        description={RISK_COPY.dangerous.description}
                        checked={displayedPermissions.dangerous_actions_require_confirm}
                        onChange={(checked) => updateCustomPermissionDraft((current) => ({
                          ...current,
                          permissions: { ...current.permissions, dangerous_actions_require_confirm: checked },
                        }))}
                      />
                      <SwitchRow
                        label="Allow screen fallback"
                        description={RISK_COPY.screenFallback.description}
                        checked={displayedPermissions.allow_screen_fallback}
                        onChange={(checked) => updateCustomPermissionDraft((current) => ({
                          ...current,
                          permissions: { ...current.permissions, allow_screen_fallback: checked },
                        }))}
                      />
                    </SettingsCard>

                    <PathOverrides
                      draft={draft}
                      updateDraft={updateCustomPermissionDraft}
                      blockedRootInput={blockedRootInput}
                      setBlockedRootInput={setBlockedRootInput}
                    />
                    <AppOverrides draft={draft} updateDraft={updateCustomPermissionDraft} />
                  </div>
                </>
              )}

              {activeTab === 'sandbox' && (
                <>
                  <PageHeader title={PAGE_COPY.sandbox.label} description={PAGE_COPY.sandbox.blurb} />
                  <div className="space-y-4">
                    <Note tone="info">{SANDBOX_COPY.intro}</Note>

                    <SettingsCard>
                      <SwitchRow
                        label="Sandbox commands"
                        description={SANDBOX_COPY.enabled}
                        checked={draft.sandbox.enabled}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          sandbox: { ...current.sandbox, enabled: checked },
                        }))}
                      />
                    </SettingsCard>

                    <SettingsCard title="Policy">
                      <SettingRow
                        label="Isolation mode"
                        description={SANDBOX_COPY.modeHelp[draft.sandbox.mode as keyof typeof SANDBOX_COPY.modeHelp] ?? SANDBOX_COPY.mode}
                      >
                        <Dropdown
                          ariaLabel="Isolation mode"
                          value={draft.sandbox.mode}
                          options={[
                            { value: 'off', label: 'Off — no shell' },
                            { value: 'auto', label: 'Automatic' },
                            { value: 'enforce', label: 'Strong isolation only' },
                            { value: 'host', label: 'Host, with approval' },
                            { value: 'docker', label: 'Docker' },
                            { value: 'local_restricted', label: 'Host, best effort' },
                          ]}
                          onChange={(mode) => updateDraft((current) => ({
                            ...current,
                            sandbox: { ...current.sandbox, mode },
                          }))}
                        />
                      </SettingRow>
                      <SettingRow
                        label="Network access"
                        description={SANDBOX_COPY.networkHelp[draft.sandbox.network.default as keyof typeof SANDBOX_COPY.networkHelp] ?? SANDBOX_COPY.network}
                      >
                        <Dropdown
                          ariaLabel="Network access"
                          value={draft.sandbox.network.default}
                          options={[
                            { value: 'deny', label: 'Blocked' },
                            { value: 'allow_with_approval', label: 'Ask first' },
                            { value: 'allow', label: 'Allowed' },
                          ]}
                          onChange={(defaultNetwork) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              network: { ...current.sandbox.network, default: defaultNetwork },
                            },
                          }))}
                        />
                      </SettingRow>
                      <SettingRow
                        label="Files created by commands"
                        description={SANDBOX_COPY.writeStrategyHelp[draft.sandbox.default_write_strategy as keyof typeof SANDBOX_COPY.writeStrategyHelp] ?? SANDBOX_COPY.writeStrategy}
                      >
                        <Dropdown
                          ariaLabel="Files created by commands"
                          value={draft.sandbox.default_write_strategy}
                          options={[
                            { value: 'discard', label: 'Discard them' },
                            { value: 'copy_out', label: 'Copy them back' },
                            { value: 'direct_rw', label: 'Write directly' },
                          ]}
                          onChange={(strategy) => updateDraft((current) => ({
                            ...current,
                            sandbox: { ...current.sandbox, default_write_strategy: strategy },
                          }))}
                        />
                      </SettingRow>
                    </SettingsCard>

                    {draft.sandbox.mode === 'enforce' && !sandboxStatus?.backends?.docker?.available && (
                      <Note tone="warn" icon={AlertCircle}>{SANDBOX_COPY.enforceNeedsDocker}</Note>
                    )}

                    <SettingsCard title="Current status">
                      <div className="st-row">
                        <div className="min-w-0">
                          <p className="st-label">Selected backend</p>
                          <p className="st-desc mt-1">
                            {sandboxStatus?.selected_backend || 'Checking…'} · {sandboxStatus?.isolation || 'unknown'} isolation
                            {sandboxStatus?.reason_code ? ` · ${sandboxStatus.reason_code}` : ''}
                          </p>
                          {sandboxStatus?.reason && (
                            <p className="st-desc mt-1">{sandboxStatus.reason}</p>
                          )}
                          {sandboxStatus?.fallback_backend && sandboxStatus.fallback_backend !== sandboxStatus.selected_backend && (
                            <p className="st-desc mt-1">
                              PowerShell fallback: {sandboxStatus.fallback_backend} · {sandboxStatus.fallback_isolation}
                              {sandboxStatus.fallback_reason_code ? ` · ${sandboxStatus.fallback_reason_code}` : ''}
                            </p>
                          )}
                        </div>
                        <div className="st-row-control-auto">
                          <Badge tone={sandboxStatus?.isolation === 'strong' ? 'ok' : 'warn'}>
                            {sandboxStatus?.isolation || 'Checking…'}
                          </Badge>
                        </div>
                      </div>
                    </SettingsCard>

                    <SettingsCard title="Available on this computer" description={SANDBOX_COPY.backends}>
                      {['docker', 'local_restricted', 'host'].map((backend) => {
                        const status = sandboxStatus?.backends?.[backend]
                        return (
                          <div key={backend} className="st-row">
                            <div className="min-w-0">
                              <p className="st-label capitalize">{backend.replace('_', ' ')}</p>
                              <p className="st-desc mt-1">
                                {status?.available ? `${status.security_label} isolation` : status?.reason || 'Checking…'}
                              </p>
                            </div>
                            <div className="st-row-control-auto">
                              <Badge tone={status?.available ? 'ok' : 'warn'}>
                                {status?.available ? 'Available' : 'Not available'}
                              </Badge>
                            </div>
                          </div>
                        )
                      })}
                    </SettingsCard>

                    <SettingsCard title="Docker">
                      <SwitchRow
                        label="Use Docker when available"
                        description="Docker gives the strongest isolation. Turn off only if it conflicts with something else on this machine."
                        checked={draft.sandbox.docker.enabled}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          sandbox: { ...current.sandbox, docker: { ...current.sandbox.docker, enabled: checked } },
                        }))}
                      />
                        <SettingRow label="Container image" description={SANDBOX_COPY.dockerImage} wide>
                        <div className="flex w-full gap-2">
                          <input
                            value={draft.sandbox.docker.image}
                            aria-label="Container image"
                            onChange={(e) => updateDraft((current) => ({
                              ...current,
                              sandbox: { ...current.sandbox, docker: { ...current.sandbox.docker, image: e.target.value } },
                            }))}
                            placeholder="python:3.12-slim@sha256:…"
                            className="st-input min-w-0 flex-1 font-mono text-xs"
                          />
                          <button
                            type="button"
                            className="st-button"
                            disabled={resolvingDockerImage}
                            onClick={async () => {
                              setResolvingDockerImage(true)
                              setDockerResolveMessage('')
                              try {
                                const result = await resolveSandboxDockerImage(draft.sandbox.docker.image)
                                updateDraft((current) => ({
                                  ...current,
                                  sandbox: { ...current.sandbox, docker: { ...current.sandbox.docker, image: result.image } },
                                }))
                                setDockerResolveMessage(result.detail)
                                setSandboxStatus(result.sandbox)
                              } catch (error) {
                                setDockerResolveMessage(error instanceof Error ? error.message : 'Docker image could not be resolved.')
                              } finally {
                                setResolvingDockerImage(false)
                              }
                            }}
                          >
                            {resolvingDockerImage ? 'Resolving…' : 'Resolve & pull'}
                          </button>
                        </div>
                        {dockerResolveMessage && <p className="st-desc mt-2">{dockerResolveMessage}</p>}
                      </SettingRow>
                      <SwitchRow
                        label="Read-only container"
                        description={SANDBOX_COPY.dockerReadOnly}
                        checked={draft.sandbox.docker.read_only_root}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          sandbox: { ...current.sandbox, docker: { ...current.sandbox.docker, read_only_root: checked } },
                        }))}
                      />
                    </SettingsCard>

                    <SettingsCard title="Resource ceilings" description={SANDBOX_COPY.resources}>
                      {([
                        { key: 'timeout_seconds' as const, label: 'Time limit', unit: 'seconds' },
                        { key: 'memory_mb' as const, label: 'Memory', unit: 'MB' },
                        { key: 'cpus' as const, label: 'CPU cores', unit: 'cores' },
                        { key: 'pids' as const, label: 'Process limit', unit: '' },
                      ]).map(({ key, label, unit }) => (
                        <NumberRow
                          key={key}
                          label={label}
                          description={SANDBOX_COPY.resourceHelp[key]}
                          ariaLabel={`Sandbox ${label.toLowerCase()}`}
                          unit={unit}
                          value={draft.sandbox.resources[key]}
                          onChange={(value) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              resources: { ...current.sandbox.resources, [key]: Number(value) },
                            },
                          }))}
                        />
                      ))}
                    </SettingsCard>
                  </div>
                </>
              )}
            </div>
          </div>

          {/* --------------------------------------------------------- footer */}
          <footer className="st-divider-t flex items-center justify-between gap-3 px-5 py-3.5">
            {confirmDiscard ? (
              <>
                <p className="st-desc">Close settings and lose your unsaved changes?</p>
                <div className="flex items-center gap-2">
                  <button type="button" onClick={() => setConfirmDiscard(false)} className="st-btn st-btn-ghost">
                    Keep editing
                  </button>
                  <button type="button" onClick={onClose} className="st-btn st-btn-danger">
                    Discard changes
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="st-desc flex items-center gap-1.5">
                  <footerMessage.icon
                    size={14}
                    className="shrink-0"
                    style={{
                      color: footerMessage.tone === 'ok'
                        ? 'var(--st-ok)'
                        : footerMessage.tone === 'warn'
                          ? 'var(--st-warn)'
                          : footerMessage.tone === 'danger'
                            ? 'var(--st-danger)'
                            : 'var(--st-text-faint)',
                    }}
                  />
                  {footerMessage.text}
                </p>
                <div className="flex items-center gap-2">
                  <button type="button" onClick={requestClose} className="st-btn st-btn-ghost">
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleSave}
                    disabled={saving || duplicateMcpNames.length > 0}
                    className="st-btn st-btn-primary"
                  >
                    <Save size={13} />
                    {saved ? 'Saved' : saving ? 'Saving…' : 'Save changes'}
                  </button>
                </div>
              </>
            )}
          </footer>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ small parts --- */

function NavItem({
  tab,
  active,
  onSelect,
}: {
  tab: SettingsTab
  active: boolean
  onSelect: () => void
}) {
  const Icon = TAB_ICONS[tab]
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'page' : undefined}
      className="st-nav-item"
    >
      <Icon size={14} className="st-nav-icon" />
      <span className="truncate">{PAGE_COPY[tab].label}</span>
    </button>
  )
}

function StatusCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="st-status-cell px-4 py-3">
      <p className="st-hint">{label}</p>
      <p className="st-label mt-1 capitalize">{value}</p>
    </div>
  )
}

function Disclosure({
  open,
  onToggle,
  title,
  description,
  children,
}: {
  open: boolean
  onToggle: () => void
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <section className="st-card">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-start justify-between gap-4 px-4 py-3.5 text-left"
      >
        <span className="min-w-0">
          <span className="st-label block">{title}</span>
          {description && <span className="st-desc mt-1 block">{description}</span>}
        </span>
        <ChevronDown
          size={14}
          className={`mt-0.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
          style={{ color: 'var(--st-text-faint)' }}
        />
      </button>
      {open && <div className="st-divider-t">{children}</div>}
    </section>
  )
}

function OutputFolderInput({
  label,
  value,
  onChange,
  onBrowse,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  onBrowse?: () => void
}) {
  return (
    <div className="flex gap-2">
      <input
        value={value}
        aria-label={label}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Folder path"
        className="st-input min-w-0 flex-1 font-mono text-xs"
      />
      {onBrowse && (
        <button type="button" onClick={onBrowse} className="st-btn st-btn-secondary shrink-0">
          <Folder size={13} />
          Browse
        </button>
      )}
    </div>
  )
}

function BrowserDiagnosticsCard({
  diagnostics,
  draft,
}: {
  diagnostics: BrowserUseDiagnostics | null
  draft: AgentSettings
}) {
  return (
    <div className="space-y-2 p-4">
      <p className="st-label">
        {diagnostics?.session_active
          ? `Session active — ${diagnostics.current_mode || 'unknown'} mode`
          : 'No active browser session'}
      </p>
      <div className="grid gap-2 sm:grid-cols-2">
        {diagnostics?.current_mode === 'system' && diagnostics.current_system_connection && (
          <ReadOnlyValue label="Connection" value={diagnostics.current_system_connection} />
        )}
        <ReadOnlyValue label="Strategy" value={diagnostics?.system_connection_strategy ?? draft.browser.system_connection_strategy} />
        <ReadOnlyValue label="Debugging address" value={diagnostics?.system_cdp_url || draft.browser.system_cdp_url} />
        {diagnostics?.chrome_executable && <ReadOnlyValue label="Chrome" value={diagnostics.chrome_executable} />}
        {diagnostics?.current_page && (
          <ReadOnlyValue label="Current page" value={diagnostics.current_page.title || diagnostics.current_page.url} />
        )}
      </div>
      {diagnostics?.last_error && <Note tone="warn">{diagnostics.last_error}</Note>}
    </div>
  )
}

function McpServerCard({
  server,
  index,
  diagnostic,
  statusText,
  bridgeEnabled,
  reconnectingServer,
  advancedOpen,
  onToggleAdvanced,
  onReconnect,
  onRemove,
  onUpdate,
}: {
  server: AgentSettings['mcp']['servers'][number]
  index: number
  diagnostic?: MCPServerDiagnostics
  statusText: string
  bridgeEnabled: boolean
  reconnectingServer: string
  advancedOpen: boolean
  onToggleAdvanced: () => void
  onReconnect: () => void
  onRemove: () => void
  onUpdate: (updater: (server: AgentSettings['mcp']['servers'][number]) => AgentSettings['mcp']['servers'][number]) => void
}) {
  const dotClass = diagnostic?.connected
    ? 'st-dot st-dot-ok'
    : diagnostic?.last_error || diagnostic?.unhealthy_reason || diagnostic?.state === 'unhealthy'
      ? 'st-dot st-dot-warn'
      : 'st-dot'
  const isHttp = server.transport === 'streamable_http'

  return (
    <section className="st-card">
      <header className="st-card-head flex items-center gap-2">
        <span className={dotClass} />
        <input
          value={server.name}
          aria-label={`Server name ${index + 1}`}
          onChange={(e) => onUpdate((current) => ({ ...current, name: e.target.value }))}
          placeholder="Server name"
          className="st-input min-w-0 flex-1 font-medium"
        />
        <Switch
          label={`Enable server ${index + 1}`}
          checked={server.enabled}
          onChange={(checked) => onUpdate((current) => ({ ...current, enabled: checked }))}
        />
        <button
          type="button"
          onClick={onRemove}
          className="st-btn st-btn-ghost st-btn-icon"
          aria-label={`Remove MCP server ${index + 1}`}
        >
          <Trash2 size={13} />
        </button>
      </header>

      <SettingRow
        label="How Monaw connects"
        description={isHttp ? MCP_COPY.transportHttp : MCP_COPY.transportStdio}
      >
        <Dropdown<AgentSettings['mcp']['servers'][number]['transport']>
          ariaLabel={`Transport for server ${index + 1}`}
          value={server.transport}
          options={[
            { value: 'stdio', label: 'Run a local program' },
            { value: 'streamable_http', label: 'Connect to a URL' },
          ]}
          onChange={(transport) => onUpdate((current) => ({ ...current, transport }))}
        />
      </SettingRow>

      <StackedRow
        label={isHttp ? 'Server URL' : 'Command'}
        description={isHttp ? MCP_COPY.url : MCP_COPY.command}
      >
        <input
          value={isHttp ? server.url : server.command}
          aria-label={isHttp ? `Server URL ${index + 1}` : `Command ${index + 1}`}
          onChange={(e) => onUpdate((current) => isHttp
            ? { ...current, url: e.target.value }
            : { ...current, command: e.target.value })}
          placeholder={isHttp ? 'http://127.0.0.1:3000/mcp' : 'npx'}
          className="st-input font-mono text-xs"
        />
      </StackedRow>

      {!isHttp && (
        <div className="st-row-stacked">
          <EditableList
            title="Arguments"
            emptyText="No arguments. Most servers need at least one."
            addLabel="Add argument"
            values={server.args}
            placeholder={(itemIndex) => `Argument ${itemIndex + 1}`}
            onAdd={() => onUpdate((current) => ({ ...current, args: [...current.args, ''] }))}
            onChange={(itemIndex, value) => onUpdate((current) => ({
              ...current,
              args: current.args.map((item, currentIndex) => currentIndex === itemIndex ? value : item),
            }))}
            onRemove={(itemIndex) => onUpdate((current) => ({
              ...current,
              args: current.args.filter((_, currentIndex) => currentIndex !== itemIndex),
            }))}
          />
        </div>
      )}

      {!isHttp && (
        <SettingRow label="Working directory" description={MCP_COPY.workingDir} wide>
          <input
            value={server.cwd}
            aria-label={`Working directory ${index + 1}`}
            onChange={(e) => onUpdate((current) => ({ ...current, cwd: e.target.value }))}
            placeholder="Optional"
            className="st-input font-mono text-xs"
          />
        </SettingRow>
      )}

      <SettingRow label="Description" description="A note for yourself. Not sent to the agent." wide>
        <input
          value={server.description}
          aria-label={`Server description ${index + 1}`}
          onChange={(e) => onUpdate((current) => ({ ...current, description: e.target.value }))}
          placeholder="Optional"
          className="st-input"
        />
      </SettingRow>

      <div className="st-card-foot flex flex-wrap items-center justify-between gap-3">
        <span
          className="st-hint"
          style={{
            color: diagnostic?.connected
              ? 'var(--st-ok)'
              : diagnostic?.last_error
                ? 'var(--st-warn)'
                : 'var(--st-text-faint)',
          }}
        >
          {statusText}
          {diagnostic?.state ? ` · ${diagnostic.state}` : ''}
        </span>
        <div className="flex items-center gap-2">
          <button type="button" onClick={onToggleAdvanced} className="st-btn st-btn-ghost">
            {advancedOpen ? 'Hide advanced' : 'Advanced'}
          </button>
          <button
            type="button"
            onClick={onReconnect}
            disabled={!server.name || reconnectingServer === server.name || !bridgeEnabled}
            className="st-btn st-btn-secondary"
          >
            {reconnectingServer === server.name ? 'Reconnecting…' : 'Reconnect'}
          </button>
        </div>
      </div>

      {advancedOpen && (
        <div className="st-divider-t">
          <NumberRow
            label="Startup timeout"
            description={MCP_COPY.startupTimeout}
            ariaLabel={`Startup timeout for server ${index + 1}`}
            unit="ms"
            min={1000}
            value={server.startup_timeout_ms}
            onChange={(value) => onUpdate((current) => ({ ...current, startup_timeout_ms: Number(value || 0) }))}
          />
          <NumberRow
            label="Call timeout"
            description={MCP_COPY.callTimeout}
            ariaLabel={`Call timeout for server ${index + 1}`}
            unit="ms"
            min={1000}
            value={server.call_timeout_ms}
            onChange={(value) => onUpdate((current) => ({ ...current, call_timeout_ms: Number(value || 0) }))}
          />
          <SwitchRow
            label="Reconnect automatically"
            description={MCP_COPY.reconnect}
            checked={server.reconnect_on_unhealthy}
            onChange={(checked) => onUpdate((current) => ({ ...current, reconnect_on_unhealthy: checked }))}
          />
          <div className="st-row-stacked">
            <EditableList
              title="Tool allow list"
              emptyText={MCP_COPY.allowList}
              addLabel="Add tool"
              values={server.allow_list}
              placeholder={() => 'Tool name'}
              onAdd={() => onUpdate((current) => ({ ...current, allow_list: [...current.allow_list, ''] }))}
              onChange={(toolIndex, value) => onUpdate((current) => ({
                ...current,
                allow_list: current.allow_list.map((item, currentIndex) => currentIndex === toolIndex ? value : item),
              }))}
              onRemove={(toolIndex) => onUpdate((current) => ({
                ...current,
                allow_list: current.allow_list.filter((_, currentIndex) => currentIndex !== toolIndex),
              }))}
            />
          </div>
          <div className="st-row-stacked">
            <EditableList
              title="Trusted tools"
              emptyText={MCP_COPY.trustedTools}
              addLabel="Add trusted tool"
              values={server.trusted_tools}
              placeholder={() => 'Remote tool name'}
              onAdd={() => onUpdate((current) => ({ ...current, trusted_tools: [...current.trusted_tools, ''] }))}
              onChange={(toolIndex, value) => onUpdate((current) => ({
                ...current,
                trusted_tools: current.trusted_tools.map((item, currentIndex) => currentIndex === toolIndex ? value : item),
              }))}
              onRemove={(toolIndex) => onUpdate((current) => ({
                ...current,
                trusted_tools: current.trusted_tools.filter((_, currentIndex) => currentIndex !== toolIndex),
              }))}
            />
          </div>
          <div className="st-row-stacked">
            <KeyValueEditor
              title="Tool risk overrides"
              addLabel="Add override"
              emptyText={MCP_COPY.riskOverrides}
              entries={server.tool_risk_overrides as Record<string, string>}
              keyPlaceholder="Remote tool name"
              valuePlaceholder="low, medium, or high"
              onAdd={() => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: { ...current.tool_risk_overrides, '': 'medium' },
              }))}
              onChange={(entryIndex, key, value) => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: renameEntry(
                  current.tool_risk_overrides as Record<string, string>,
                  entryIndex,
                  key,
                  value,
                ) as AgentSettings['mcp']['servers'][number]['tool_risk_overrides'],
              }))}
              onRemove={(entryIndex) => onUpdate((current) => ({
                ...current,
                tool_risk_overrides: removeEntry(
                  current.tool_risk_overrides as Record<string, string>,
                  entryIndex,
                ) as AgentSettings['mcp']['servers'][number]['tool_risk_overrides'],
              }))}
            />
          </div>
          <div className="st-row-stacked">
            {isHttp ? (
              <KeyValueEditor
                title="HTTP headers"
                addLabel="Add header"
                emptyText="No headers configured."
                entries={server.headers}
                keyPlaceholder="Header"
                valuePlaceholder="Value"
                onAdd={() => onUpdate((current) => ({ ...current, headers: { ...current.headers, '': '' } }))}
                onChange={(entryIndex, key, value) => onUpdate((current) => ({
                  ...current,
                  headers: renameEntry(current.headers, entryIndex, key, value),
                }))}
                onRemove={(entryIndex) => onUpdate((current) => ({
                  ...current,
                  headers: removeEntry(current.headers, entryIndex),
                }))}
              />
            ) : (
              <KeyValueEditor
                title="Environment variables"
                addLabel="Add variable"
                emptyText="No environment variables configured."
                entries={server.env}
                keyPlaceholder="Name"
                valuePlaceholder="Value"
                onAdd={() => onUpdate((current) => ({ ...current, env: { ...current.env, '': '' } }))}
                onChange={(entryIndex, key, value) => onUpdate((current) => ({
                  ...current,
                  env: renameEntry(current.env, entryIndex, key, value),
                }))}
                onRemove={(entryIndex) => onUpdate((current) => ({
                  ...current,
                  env: removeEntry(current.env, entryIndex),
                }))}
              />
            )}
          </div>
          {diagnostic && (
            <div className="st-row-stacked">
              <McpDiagnostics diagnostic={diagnostic} />
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/** Replace the entry at `index`, preserving key order. */
function renameEntry(
  entries: Record<string, string>,
  index: number,
  key: string,
  value: string,
): Record<string, string> {
  const next: Record<string, string> = {}
  Object.entries(entries).forEach(([entryKey, entryValue], currentIndex) => {
    if (currentIndex === index) next[key] = value
    else next[entryKey] = entryValue
  })
  return next
}

function removeEntry(entries: Record<string, string>, index: number): Record<string, string> {
  const next: Record<string, string> = {}
  Object.entries(entries).forEach(([entryKey, entryValue], currentIndex) => {
    if (currentIndex !== index) next[entryKey] = entryValue
  })
  return next
}

function EditableList({
  title,
  emptyText,
  addLabel,
  values,
  placeholder,
  onAdd,
  onChange,
  onRemove,
}: {
  title: string
  emptyText: string
  addLabel: string
  values: string[]
  placeholder: (index: number) => string
  onAdd: () => void
  onChange: (index: number, value: string) => void
  onRemove: (index: number) => void
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="st-label">{title}</p>
        <button type="button" onClick={onAdd} className="st-btn st-btn-ghost">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {values.length === 0 ? (
        <p className="st-hint">{emptyText}</p>
      ) : (
        values.map((value, itemIndex) => (
          <div key={`${title}-${itemIndex}`} className="flex gap-2">
            <input
              value={value}
              aria-label={`${title} ${itemIndex + 1}`}
              onChange={(e) => onChange(itemIndex, e.target.value)}
              placeholder={placeholder(itemIndex)}
              className="st-input min-w-0 flex-1 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => onRemove(itemIndex)}
              className="st-btn st-btn-ghost st-btn-icon shrink-0"
              aria-label={`Remove ${title} item ${itemIndex + 1}`}
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

function KeyValueEditor({
  title,
  addLabel,
  emptyText,
  entries,
  keyPlaceholder,
  valuePlaceholder,
  onAdd,
  onChange,
  onRemove,
}: {
  title: string
  addLabel: string
  emptyText: string
  entries: Record<string, string>
  keyPlaceholder: string
  valuePlaceholder: string
  onAdd: () => void
  onChange: (entryIndex: number, key: string, value: string) => void
  onRemove: (entryIndex: number) => void
}) {
  const entryList = Object.entries(entries)
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="st-label">{title}</p>
        <button type="button" onClick={onAdd} className="st-btn st-btn-ghost">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {entryList.length === 0 ? (
        <p className="st-hint">{emptyText}</p>
      ) : (
        entryList.map(([entryKey, entryValue], entryIndex) => (
          <div key={`${title}-${entryIndex}`} className="grid grid-cols-[1fr_1fr_auto] gap-2">
            <input
              value={entryKey}
              aria-label={`${title} name ${entryIndex + 1}`}
              onChange={(e) => onChange(entryIndex, e.target.value, entryValue)}
              placeholder={keyPlaceholder}
              className="st-input min-w-0 font-mono text-xs"
            />
            <input
              value={entryValue}
              aria-label={`${title} value ${entryIndex + 1}`}
              onChange={(e) => onChange(entryIndex, entryKey, e.target.value)}
              placeholder={valuePlaceholder}
              className="st-input min-w-0 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => onRemove(entryIndex)}
              className="st-btn st-btn-ghost st-btn-icon"
              aria-label={`Remove ${title} item ${entryIndex + 1}`}
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

function McpDiagnostics({ diagnostic }: { diagnostic: MCPServerDiagnostics }) {
  return (
    <div className="space-y-2">
      <p className="st-label">Diagnostics</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <ReadOnlyValue
          label={diagnostic.transport === 'streamable_http' ? 'URL' : 'Command'}
          value={diagnostic.transport === 'streamable_http'
            ? diagnostic.url || ''
            : [diagnostic.command, ...(diagnostic.args || [])].filter(Boolean).join(' ')}
        />
        <ReadOnlyValue label="Working directory" value={diagnostic.cwd || ''} />
        <ReadOnlyValue label="Executable" value={diagnostic.resolved_executable || ''} />
        <ReadOnlyValue label="Process ID" value={diagnostic.pid ? String(diagnostic.pid) : ''} />
        <ReadOnlyValue label="Connected at" value={diagnostic.connected_at || ''} />
        <ReadOnlyValue
          label="Last call"
          value={diagnostic.last_call_duration_ms ? `${diagnostic.last_call_duration_ms} ms` : ''}
        />
        <ReadOnlyValue label="Failed calls" value={String(diagnostic.failed_call_count)} />
        <ReadOnlyValue label="Tools exposed" value={String(diagnostic.reflected_tool_names?.length ?? 0)} />
      </div>
      {diagnostic.unhealthy_reason && <Note tone="warn">{diagnostic.unhealthy_reason}</Note>}
      {(diagnostic.reflected_tool_names?.length ?? 0) > 0 && (
        <ReadOnlyValue label="Tool names" value={diagnostic.reflected_tool_names.join(', ')} />
      )}
      {diagnostic.stderr_tail && (
        <pre className="st-code max-h-32 overflow-auto whitespace-pre-wrap">{diagnostic.stderr_tail}</pre>
      )}
    </div>
  )
}

function PathOverrides({
  draft,
  updateDraft,
  blockedRootInput,
  setBlockedRootInput,
}: {
  draft: AgentSettings
  updateDraft: (updater: (current: AgentSettings) => AgentSettings) => void
  blockedRootInput: string
  setBlockedRootInput: (value: string) => void
}) {
  return (
    <SettingsCard
      title="Folder rules"
      description={OVERRIDE_COPY.paths}
      action={
        <button
          type="button"
          onClick={() => updateDraft((current) => ({
            ...current,
            permissions: {
              ...current.permissions,
              path_rules: [...current.permissions.path_rules, emptyPathRule()],
            },
          }))}
          className="st-btn st-btn-secondary"
        >
          <Plus size={13} />
          Add folder
        </button>
      }
    >
      {draft.permissions.path_rules.length === 0 ? (
        <p className="st-hint p-4">No folder rules. The profile above applies everywhere.</p>
      ) : (
        draft.permissions.path_rules.map((rule, index) => (
          <div key={`path-rule-${index}`} className="st-row-stacked">
            <div className="flex gap-2">
              <input
                value={rule.path}
                aria-label={`Folder path ${index + 1}`}
                onChange={(e) => updateDraft((current) => {
                  const pathRules = [...current.permissions.path_rules]
                  pathRules[index] = { ...pathRules[index], path: e.target.value }
                  return { ...current, permissions: { ...current.permissions, path_rules: pathRules } }
                })}
                placeholder="C:\Users\you\Projects"
                className="st-input min-w-0 flex-1 font-mono text-xs"
              />
              <button
                type="button"
                onClick={() => updateDraft((current) => ({
                  ...current,
                  permissions: {
                    ...current.permissions,
                    path_rules: current.permissions.path_rules.filter((_, pathIndex) => pathIndex !== index),
                  },
                }))}
                className="st-btn st-btn-ghost st-btn-icon shrink-0"
                aria-label={`Remove path override ${index + 1}`}
              >
                <Trash2 size={13} />
              </button>
            </div>
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {(Object.keys(OVERRIDE_COPY.pathFlags) as Array<keyof typeof OVERRIDE_COPY.pathFlags>).map((key) => (
                <FlagToggle
                  key={key}
                  label={OVERRIDE_COPY.pathFlags[key]}
                  ariaLabel={`${OVERRIDE_COPY.pathFlags[key]} for folder ${index + 1}`}
                  checked={rule[key]}
                  onChange={(checked) => updateDraft((current) => {
                    const pathRules = [...current.permissions.path_rules]
                    pathRules[index] = { ...pathRules[index], [key]: checked }
                    return { ...current, permissions: { ...current.permissions, path_rules: pathRules } }
                  })}
                />
              ))}
            </div>
          </div>
        ))
      )}

      <div className="st-row-stacked">
        <p className="st-label">Never allowed</p>
        <p className="st-desc mt-1">{OVERRIDE_COPY.blockedRoots}</p>
        <div className="mt-2 space-y-2">
          {draft.permissions.blocked_roots.map((root, index) => (
            <div key={`blocked-root-${index}`} className="flex gap-2">
              <input
                value={root}
                aria-label={`Blocked folder ${index + 1}`}
                onChange={(e) => updateDraft((current) => {
                  const blockedRoots = [...current.permissions.blocked_roots]
                  blockedRoots[index] = e.target.value
                  return { ...current, permissions: { ...current.permissions, blocked_roots: blockedRoots } }
                })}
                className="st-input min-w-0 flex-1 font-mono text-xs"
              />
              <button
                type="button"
                onClick={() => updateDraft((current) => ({
                  ...current,
                  permissions: {
                    ...current.permissions,
                    blocked_roots: current.permissions.blocked_roots.filter((_, rootIndex) => rootIndex !== index),
                  },
                }))}
                className="st-btn st-btn-ghost st-btn-icon shrink-0"
                aria-label={`Remove blocked root ${index + 1}`}
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
          <div className="flex gap-2">
            <input
              value={blockedRootInput}
              aria-label="New blocked folder"
              onChange={(e) => setBlockedRootInput(e.target.value)}
              placeholder="Add a folder the agent must never touch"
              className="st-input min-w-0 flex-1 font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => {
                if (!blockedRootInput.trim()) return
                updateDraft((current) => ({
                  ...current,
                  permissions: {
                    ...current.permissions,
                    blocked_roots: [...current.permissions.blocked_roots, blockedRootInput.trim()],
                  },
                }))
                setBlockedRootInput('')
              }}
              className="st-btn st-btn-primary st-btn-icon shrink-0"
              aria-label="Add blocked root"
            >
              <Plus size={14} />
            </button>
          </div>
        </div>
      </div>
    </SettingsCard>
  )
}

function AppOverrides({
  draft,
  updateDraft,
}: {
  draft: AgentSettings
  updateDraft: (updater: (current: AgentSettings) => AgentSettings) => void
}) {
  return (
    <SettingsCard
      title="Application rules"
      description={OVERRIDE_COPY.apps}
      action={
        <button
          type="button"
          onClick={() => updateDraft((current) => ({
            ...current,
            permissions: {
              ...current.permissions,
              app_rules: [...current.permissions.app_rules, emptyAppRule()],
            },
          }))}
          className="st-btn st-btn-secondary"
        >
          <Plus size={13} />
          Add app
        </button>
      }
    >
      {draft.permissions.app_rules.length === 0 ? (
        <p className="st-hint p-4">No application rules. The profile above applies to every app.</p>
      ) : (
        draft.permissions.app_rules.map((rule, index) => (
          <div key={`app-rule-${index}`} className="st-row-stacked">
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                value={rule.alias}
                aria-label={`App alias ${index + 1}`}
                onChange={(e) => updateDraft((current) => {
                  const appRules = [...current.permissions.app_rules]
                  appRules[index] = { ...appRules[index], alias: e.target.value }
                  return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
                })}
                placeholder="Alias"
                className="st-input"
              />
              <input
                value={rule.display_name}
                aria-label={`App display name ${index + 1}`}
                onChange={(e) => updateDraft((current) => {
                  const appRules = [...current.permissions.app_rules]
                  appRules[index] = { ...appRules[index], display_name: e.target.value }
                  return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
                })}
                placeholder="Display name"
                className="st-input"
              />
            </div>
            <input
              value={rule.exe_paths.join(', ')}
              aria-label={`App executable paths ${index + 1}`}
              onChange={(e) => updateDraft((current) => {
                const appRules = [...current.permissions.app_rules]
                appRules[index] = {
                  ...appRules[index],
                  exe_paths: e.target.value.split(',').map((path) => path.trim()).filter(Boolean),
                }
                return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
              })}
              placeholder="Executable paths, separated by commas"
              className="st-input mt-2 font-mono text-xs"
            />
            <div className="mt-3 grid gap-2 sm:grid-cols-2">
              {(Object.keys(OVERRIDE_COPY.appFlags) as Array<keyof typeof OVERRIDE_COPY.appFlags>).map((key) => (
                <FlagToggle
                  key={key}
                  label={OVERRIDE_COPY.appFlags[key]}
                  ariaLabel={`${OVERRIDE_COPY.appFlags[key]} for app ${index + 1}`}
                  checked={rule[key]}
                  onChange={(checked) => updateDraft((current) => {
                    const appRules = [...current.permissions.app_rules]
                    appRules[index] = { ...appRules[index], [key]: checked }
                    return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
                  })}
                />
              ))}
            </div>
            <button
              type="button"
              onClick={() => updateDraft((current) => ({
                ...current,
                permissions: {
                  ...current.permissions,
                  app_rules: current.permissions.app_rules.filter((_, appIndex) => appIndex !== index),
                },
              }))}
              className="st-btn st-btn-danger mt-3"
            >
              <Trash2 size={12} />
              Remove this app rule
            </button>
          </div>
        ))
      )}
    </SettingsCard>
  )
}
