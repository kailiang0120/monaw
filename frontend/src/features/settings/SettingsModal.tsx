import { useEffect, useMemo, useState, type ReactNode } from 'react'
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
  Server,
  Shield,
  Trash2,
  UserRound,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react'
import { MemorySettingsPanel } from './MemorySettingsPanel'
import { ObservabilityPanel } from './ObservabilityPanel'
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
  PERMISSION_MODE_HELP,
  PERMISSION_PROFILES,
  SKILL_GROUP_COPY,
  SKILL_GUIDANCE,
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
  visionFallbackModels,
  type ModelOptionsCatalog,
  type MCPServerTemplateKey,
} from './settingsConfig'

interface Props {
  onClose: () => void
}

type SettingsTab = 'model' | 'apiKeys' | 'identity' | 'memory' | 'skills' | 'browser' | 'mcp' | 'observability' | 'permissions' | 'sandbox'

const PERMISSION_MODE_LABEL: Record<AgentSettings['permissions']['mode'], string> = {
  default: 'Default',
  full_access: 'Full Access',
  custom: 'Custom',
}

const SETTINGS_TABS: Array<{
  id: SettingsTab
  label: string
  description: string
  icon: LucideIcon
}> = [
  { id: 'model', label: 'Model', description: 'Provider and runtime', icon: Bot },
  { id: 'apiKeys', label: 'Connections', description: 'Portals and tokens', icon: KeyRound },
  { id: 'identity', label: 'Identity', description: 'Names and tone', icon: UserRound },
  { id: 'memory', label: 'Memory', description: 'User behavior', icon: Brain },
  { id: 'skills', label: 'Skills', description: 'Agent capabilities', icon: Wrench },
  { id: 'browser', label: 'Browser', description: 'Browser-use runtime', icon: Globe2 },
  { id: 'mcp', label: 'MCP', description: 'External tool servers', icon: Server },
  { id: 'observability', label: 'Observability', description: 'Traces and logs', icon: Activity },
  { id: 'permissions', label: 'Permissions', description: 'Approvals and overrides', icon: Shield },
  { id: 'sandbox', label: 'Sandbox', description: 'Exec isolation', icon: Container },
]

export function SettingsModal({ onClose }: Props) {
  const [draft, setDraft] = useState<AgentSettings | null>(null)
  const [modelOptions, setModelOptions] = useState<ModelOptionsCatalog>(FALLBACK_MODEL_OPTIONS)
  const [activeTab, setActiveTab] = useState<SettingsTab>('model')
  const [openaiKey, setOpenaiKey] = useState('')
  const [deepseekKey, setDeepseekKey] = useState('')
  const [tavilyKey, setTavilyKey] = useState('')
  const [googleKey, setGoogleKey] = useState('')
  const [telegramBotToken, setTelegramBotToken] = useState('')
  const [telegramAllowedUserIds, setTelegramAllowedUserIds] = useState('')
  const [telegramAllowedChatIds, setTelegramAllowedChatIds] = useState('')
  const [dirtySecrets, setDirtySecrets] = useState<Set<ConnectionSecretId>>(() => new Set())
  const [dirtyTelegramAllowlist, setDirtyTelegramAllowlist] = useState(false)
  const [showKey, setShowKey] = useState(false)
  const [activeConnectionPortal, setActiveConnectionPortal] = useState<ConnectionPortalId>(DEFAULT_CONNECTION_PORTAL)
  const [mcpTemplate, setMcpTemplate] = useState<MCPServerTemplateKey>('filesystem')
  const [browserDiagnostics, setBrowserDiagnostics] = useState<BrowserUseDiagnostics | null>(null)
  const [mcpDiagnostics, setMcpDiagnostics] = useState<Record<string, MCPServerDiagnostics>>({})
  const [sandboxStatus, setSandboxStatus] = useState<SandboxStatus | null>(null)
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

  const storeMcpDiagnostics = (items: MCPServerDiagnostics[]) => {
    const next: Record<string, MCPServerDiagnostics> = {}
    items.forEach((item) => {
      next[item.name] = item
    })
    setMcpDiagnostics(next)
  }

  const loadMcpDiagnostics = async () => {
    try {
      storeMcpDiagnostics(await fetchMCPDiagnostics())
    } catch {
      setMcpDiagnostics({})
    }
  }

  const loadBrowserDiagnostics = async () => {
    try {
      setBrowserDiagnostics(await fetchBrowserUseDiagnostics())
    } catch {
      setBrowserDiagnostics(null)
    }
  }

  const loadSandboxStatus = async () => {
    try {
      setSandboxStatus(await fetchSandboxStatus())
    } catch {
      setSandboxStatus(null)
    }
  }

  const loadSpeechToTextStatus = async () => {
    try {
      setSpeechToTextStatus(await fetchSpeechToTextStatus())
      setSpeechToTextError('')
    } catch (error) {
      setSpeechToTextStatus(null)
      setSpeechToTextError(error instanceof Error ? error.message : 'Speech-to-text status could not be loaded.')
    }
  }

  const loadWorkspaceInstructions = async () => {
    try {
      const instructions = await fetchWorkspaceInstructions()
      setCustomInstructions(instructions.content)
      setCustomInstructionsSavedContent(instructions.content)
      setCustomInstructionsPath(instructions.path)
      setCustomInstructionsError('')
    } catch (error) {
      setCustomInstructions('')
      setCustomInstructionsSavedContent('')
      setCustomInstructionsPath('')
      setCustomInstructionsError(error instanceof Error ? error.message : 'Custom instructions could not be loaded.')
    }
  }

  useEffect(() => {
    let cancelled = false

    const load = async () => {
      const modelOptionsPromise = fetchModelOptions().catch(() => FALLBACK_MODEL_OPTIONS)
      modelOptionsPromise.then((loadedModelOptions) => {
        if (cancelled) return
        setModelOptions(loadedModelOptions)
        setDraft((current) => (current ? normalizeDraft(current, loadedModelOptions) : current))
      })

      let settings: AgentSettings | null = null
      try {
        settings = normalizeDraft(await fetchSettings())
        if (!cancelled) {
          setLoadError('')
          setDraft(settings)
        }
      } catch (error) {
        console.error('[settings] Failed to load settings', error)
        if (!cancelled) {
          setLoadError(error instanceof Error ? error.message : 'Settings could not be loaded.')
        }
        return
      }

      void Promise.all([
        loadMcpDiagnostics(),
        loadBrowserDiagnostics(),
        loadSandboxStatus(),
        loadSpeechToTextStatus(),
        loadWorkspaceInstructions(),
      ])

      if (window.electronAPI) {
        try {
          const {
            openaiKey: oai,
            deepseekKey: dsk,
            googleKey: ggl,
            tavilyKey: tvly,
            telegramBotToken: tbot,
            telegramAllowedUserIds: tUsers,
            telegramAllowedChatIds: tChats,
          } = await syncStoredApiKeysToBackend()
          if (cancelled) return
          setOpenaiKey(oai)
          setDeepseekKey(dsk)
          setGoogleKey(ggl)
          setTavilyKey(tvly)
          setTelegramBotToken(tbot)
          setTelegramAllowedUserIds(tUsers)
          setTelegramAllowedChatIds(tChats)
          setDirtySecrets(new Set())
          setDirtyTelegramAllowlist(false)
          setDraft((current) => current ? {
            ...current,
            telegram_allowed_user_ids: tUsers || current.telegram_allowed_user_ids,
            telegram_allowed_chat_ids: tChats || current.telegram_allowed_chat_ids,
            api_keys: {
              ...current.api_keys,
              has_openai_key: current.api_keys.has_openai_key || Boolean(oai),
              has_deepseek_key: current.api_keys.has_deepseek_key || Boolean(dsk),
              has_google_key: current.api_keys.has_google_key || Boolean(ggl),
              has_tavily_key: current.api_keys.has_tavily_key || Boolean(tvly),
              has_telegram_bot_token: current.api_keys.has_telegram_bot_token || Boolean(tbot),
              has_telegram_allowlist: current.api_keys.has_telegram_allowlist || Boolean(tUsers || tChats),
            },
          } : current)
        } catch {
          // Stored key sync should not prevent the settings UI from opening.
        }
      } else if (settings) {
        setTelegramAllowedUserIds(settings.telegram_allowed_user_ids)
        setTelegramAllowedChatIds(settings.telegram_allowed_chat_ids)
      }
    }
    load()

    return () => {
      cancelled = true
    }
  }, [])

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
    if (!window.electronAPI) return
    const writes: Array<[ConnectionSecretId, string, string]> = [
      ['openai', 'openai_api_key', openaiKey],
      ['deepseek', 'deepseek_api_key', deepseekKey],
      ['google', 'google_api_key', googleKey],
      ['tavily', 'tavily_api_key', tavilyKey],
      ['telegramBot', 'telegram_bot_token', telegramBotToken],
    ]
    for (const [secret, storeKey, value] of writes) {
      if (!dirtySecrets.has(secret)) continue
      if (value) await window.electronAPI.storeSet(storeKey, value)
      else await window.electronAPI.storeDelete(storeKey)
    }
    if (dirtyTelegramAllowlist) {
      const userIds = telegramAllowedUserIds.trim()
      const chatIds = telegramAllowedChatIds.trim()
      if (userIds) await window.electronAPI.storeSet('telegram_allowed_user_ids', userIds)
      else await window.electronAPI.storeDelete('telegram_allowed_user_ids')
      if (chatIds) await window.electronAPI.storeSet('telegram_allowed_chat_ids', chatIds)
      else await window.electronAPI.storeDelete('telegram_allowed_chat_ids')
    }
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
      case 'deepseek':
        setDeepseekKey(value)
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
    try {
      const secretPayload: {
        openai_api_key?: string
        deepseek_api_key?: string
        google_api_key?: string
        tavily_api_key?: string
        telegram_bot_token?: string
      } = {}
      if (dirtySecrets.has('openai')) secretPayload.openai_api_key = openaiKey
      if (dirtySecrets.has('deepseek')) secretPayload.deepseek_api_key = deepseekKey
      if (dirtySecrets.has('google')) secretPayload.google_api_key = googleKey
      if (dirtySecrets.has('tavily')) secretPayload.tavily_api_key = tavilyKey
      if (dirtySecrets.has('telegramBot')) secretPayload.telegram_bot_token = telegramBotToken

      const savedSettings = await updateSettings({
        llm: draft.llm,
        speech_to_text: draft.speech_to_text,
        mcp: draft.mcp,
        browser: draft.browser,
        memory: draft.memory,
        tools: draft.tools,
        permissions: draft.permissions,
        sandbox: draft.sandbox,
        identity: draft.identity,
        deepseek_base_url: 'https://api.deepseek.com',
        telegram_allowed_user_ids: telegramAllowedUserIds.trim(),
        telegram_allowed_chat_ids: telegramAllowedChatIds.trim(),
        ...secretPayload,
      })
      const normalizedSavedSettings = normalizeDraft(savedSettings, modelOptions)
      setDraft(normalizedSavedSettings)
      setTelegramAllowedUserIds(normalizedSavedSettings.telegram_allowed_user_ids)
      setTelegramAllowedChatIds(normalizedSavedSettings.telegram_allowed_chat_ids)
      await saveKeys()
      setDirtySecrets(new Set())
      setDirtyTelegramAllowlist(false)
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

  const draftStats = useMemo(() => {
    if (!draft) return null
    const enabledSkills = Object.values(draft.tools.skills).filter(Boolean).length
    const connectedMcp = Object.values(mcpDiagnostics).filter((item) => item.connected).length
    return {
      enabledSkills,
      serverCount: draft.mcp.servers.length,
      connectedMcp,
      permissionMode: PERMISSION_MODE_LABEL[draft.permissions.mode],
    }
  }, [draft, mcpDiagnostics])

  if (!draft) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
        <div className="panel max-w-md rounded-2xl px-4 py-3 text-sm text-neutral-300">
          {loadError ? (
            <div>
              <p className="font-medium text-red-200">Settings failed to load.</p>
              <p className="mt-1 text-xs text-neutral-400">{loadError}</p>
            </div>
          ) : (
            'Loading settings...'
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
    recommended: draft.available_skills.filter((skill) => skill.tier === 'recommended'),
    optional: draft.available_skills.filter((skill) => skill.tier === 'optional'),
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
    deepseek: deepseekKey,
    google: googleKey,
    tavily: tavilyKey,
    telegramBot: telegramBotToken,
  }
  const connectionSecretStatuses: ConnectionSecretStatuses = {
    openai: draft.api_keys.has_openai_key,
    deepseek: draft.api_keys.has_deepseek_key,
    google: draft.api_keys.has_google_key,
    tavily: draft.api_keys.has_tavily_key,
    telegramBot: Boolean(draft.api_keys.has_telegram_bot_token),
  }
  const telegramAllowlistConfigured = Boolean(telegramAllowedUserIds.trim() || telegramAllowedChatIds.trim())
  const customInstructionsDirty = customInstructions !== customInstructionsSavedContent
  const speechToTextIsCloud = draft.speech_to_text.engine === 'cloud'
  const speechToTextReady = speechToTextIsCloud ? Boolean(speechToTextStatus?.cloud_configured) : Boolean(speechToTextStatus?.downloaded)
  const speechToTextHeaderStatus = speechToTextIsCloud
    ? (speechToTextReady ? 'cloud ready' : 'key needed')
    : (speechToTextReady ? 'ready' : 'download needed')
  const speechToTextHeaderClass = speechToTextReady
    ? 'bg-emerald-400/10 text-emerald-200'
    : 'bg-amber-400/10 text-amber-200'
  const speechToTextPanelStatus = speechToTextIsCloud
    ? (speechToTextStatus?.cloud_configured ? 'Uses your saved Google API key.' : 'Save a Google API key in Connections.')
    : deletingSpeechToText
      ? 'Deleting local speech model...'
      : offloadingSpeechToText
        ? 'Offloading speech model...'
        : speechToTextStatus?.loaded
          ? 'Model is loaded in memory.'
          : speechToTextStatus?.downloaded
            ? 'Local transcription is ready.'
            : downloadingSpeechToText
              ? 'Downloading Whisper base...'
              : 'Download once before voice input.'

  const getSkillHealth = (skill: AgentSettings['available_skills'][number]) => {
    if (!skill.available) return `Unavailable: ${formatUnavailableReason(skill.unavailable_reason)}`
    if (skill.name === 'browser-use') {
      if (!browserSkillEnabled) return draft.tools.skills['browser-use'] ? 'Unavailable' : 'Disabled'
      if (!browserDiagnostics) return 'Waiting for diagnostics'
      if (browserDiagnostics.session_active && browserDiagnostics.current_mode) {
        return browserDiagnostics.current_mode === 'managed' ? 'Managed active' : 'System active'
      }
      if (browserDiagnostics.last_error) return 'Degraded'
      return 'Ready'
    }
    return skill.recommended ? 'Default on' : 'Optional'
  }

  const skillHealthClass = (skill: AgentSettings['available_skills'][number]) => {
    const health = getSkillHealth(skill)
    if (health.startsWith('Unavailable') || health === 'Degraded') {
      return 'border-amber-400/25 bg-amber-400/10 text-amber-300'
    }
    if (
      health === 'Connected' ||
      health === 'Ready' ||
      health === 'Default on' ||
      health === 'Managed active' ||
      health === 'System active'
    ) {
      return 'border-emerald-400/25 bg-emerald-400/10 text-emerald-300'
    }
    return 'border-white/[0.08] bg-white/[0.03] text-neutral-400'
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="panel flex h-[min(92vh,920px)] w-full max-w-6xl overflow-hidden rounded-2xl">
        <aside className="settings-sidebar hidden h-full w-48 shrink-0 flex-col border-r border-white/[0.07] bg-[#141312] p-3 md:flex">
          <div className="mb-4 flex items-center justify-between gap-2">
            <div>
              <p className="section-label">Agent controls</p>
              <h2 className="mt-0.5 text-base font-semibold tracking-tight text-neutral-100">Settings</h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="ghost-button h-7 w-7 rounded-lg"
              aria-label="Close settings"
            >
              <X size={14} />
            </button>
          </div>

          <div className="settings-sidebar-scroll min-h-0 flex-1 overflow-y-auto">
            <nav className="space-y-0.5">
              {SETTINGS_TABS.map((tab) => {
                const Icon = tab.icon
                const active = activeTab === tab.id
                return (
                  <button
                    key={tab.id}
                    type="button"
                    onClick={() => setActiveTab(tab.id)}
                    className={`flex w-full items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left text-xs transition-colors ${
                      active
                        ? 'border-accent/25 bg-accent/10 font-medium text-neutral-100'
                        : 'border-transparent font-medium text-neutral-500 hover:border-white/[0.08] hover:bg-white/[0.035] hover:text-neutral-200'
                    }`}
                  >
                    <Icon size={14} className={active ? 'shrink-0 text-accent-light' : 'shrink-0 text-neutral-600'} />
                    <span className="truncate">{tab.label}</span>
                  </button>
                )
              })}
            </nav>

            {draftStats && (
              <div className="mt-4 rounded-lg border border-white/[0.07] bg-white/[0.025] p-2.5">
                <p className="section-label mb-2">Snapshot</p>
                <div className="space-y-1.5 text-[11px] text-neutral-400">
                  <SettingStat label="Skills on" value={draftStats.enabledSkills} />
                  <SettingStat label="MCP" value={draftStats.serverCount} />
                  <SettingStat label="Connected" value={draftStats.connectedMcp} />
                  <SettingStat label="Mode" value={draftStats.permissionMode} />
                </div>
              </div>
            )}
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center justify-between gap-3 border-b border-white/[0.07] px-5 py-4 md:hidden">
            <div>
              <p className="section-label">Agent controls</p>
              <h2 className="mt-1 text-base font-semibold text-neutral-100">Settings</h2>
            </div>
            <button
              type="button"
              onClick={onClose}
              className="ghost-button h-8 w-8 rounded-lg"
              aria-label="Close settings"
            >
              <X size={15} />
            </button>
          </header>

          <div className="border-b border-white/[0.07] px-4 py-3 md:hidden">
            <select
              value={activeTab}
              onChange={(e) => setActiveTab(e.target.value as SettingsTab)}
              className="control w-full rounded-xl px-3 py-2 text-sm"
            >
              {SETTINGS_TABS.map((tab) => (
                <option key={tab.id} value={tab.id}>{tab.label}</option>
              ))}
            </select>
          </div>

          <div className="settings-scroll-area min-h-0 flex-1 overflow-y-auto px-5 pb-0 pt-5">
            {activeTab === 'model' && (
              <SettingsPanel
                icon={Bot}
                title="Model"
                description="Configure provider, model selection, reasoning effort, and long-running turn limits."
              >
                <div className="space-y-4">
                    <div className="rounded-lg border border-accent/20 bg-accent/10 px-3 py-2 text-xs text-neutral-400">
                      Choose OpenAI, DeepSeek, or Google. DeepSeek uses the OpenAI-compatible endpoint at https://api.deepseek.com.
                    </div>

                    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                      <div className="space-y-3">
                        <Field label="Provider">
                          <Dropdown<AgentSettings['llm']['provider']>
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
                        </Field>
                        <Field label="Model">
                          <Dropdown<string>
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
                        </Field>
                        <Field label="Reasoning effort">
                          <Dropdown<AgentSettings['llm']['reasoning_effort']>
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
                        </Field>
                        <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                          <div className="space-y-3">
                            <ToggleRow
                              label="Vision fallback"
                              checked={draft.llm.vision_fallback_enabled}
                              onChange={(vision_fallback_enabled) => updateDraft((current) => ({
                                ...current,
                                llm: { ...current.llm, vision_fallback_enabled },
                              }))}
                            />
                            <Field label="Vision model">
                              <Dropdown<string>
                                value={draft.llm.vision_fallback_model}
                                options={visionFallbackModels(modelOptions).map((m) => ({ value: m, label: m }))}
                                disabled={!draft.llm.vision_fallback_enabled}
                                onChange={(vision_fallback_model) => updateDraft((current) => ({
                                  ...current,
                                  llm: { ...current.llm, vision_fallback_model },
                                }))}
                              />
                            </Field>
                          </div>
                          <p className="mt-2 text-xs leading-relaxed text-neutral-500">
                            Used when the active model cannot read screenshots. Requires a saved Google API key.
                          </p>
                        </div>
                      </div>

                      <div className="space-y-4">
                        <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                          {/* Header */}
                          <div className="mb-3 flex items-center justify-between gap-3">
                            <div className="flex items-center gap-2">
                              <Mic size={14} className="text-accent-light" />
                              <p className="text-xs font-semibold text-neutral-200">Speech to Text</p>
                            </div>
                            <span className={`status-pill border-white/[0.08] ${speechToTextHeaderClass}`}>
                              {speechToTextHeaderStatus}
                            </span>
                          </div>
                          {/* Engine selector — inline */}
                          <div className="mb-3 flex items-center justify-between gap-3">
                            <span className="text-xs text-neutral-500">Engine</span>
                            <Dropdown<AgentSettings['speech_to_text']['engine']>
                              size="sm"
                              className="w-28"
                              value={draft.speech_to_text.engine}
                              options={[
                                { value: 'local', label: 'Local' },
                                { value: 'cloud', label: 'Cloud' },
                              ]}
                              onChange={(engine) => updateDraft((current) => ({
                                ...current,
                                speech_to_text: { ...current.speech_to_text, engine },
                              }))}
                            />
                          </div>
                          {/* Single compact row: model info + status badge + actions */}
                          <div className="flex items-center justify-between gap-3 rounded-lg border border-white/[0.06] bg-white/[0.025] px-3 py-2">
                            <div className="min-w-0">
                              <p className="truncate text-[11px] font-medium text-neutral-200">
                                {speechToTextIsCloud
                                  ? draft.speech_to_text.cloud_model
                                  : (speechToTextStatus ? `${speechToTextStatus.model_label} (${speechToTextStatus.model_size})` : 'Whisper base (~142 MB)')}
                              </p>
                              <p className="mt-0.5 text-[10px] text-neutral-500">{speechToTextPanelStatus}</p>
                            </div>
                            <div className="flex shrink-0 items-center gap-1.5">
                              {speechToTextStatus?.loaded && (
                                <span className="status-pill border-white/[0.08] bg-emerald-400/10 text-[10px] text-emerald-200">loaded</span>
                              )}
                              <button
                                type="button"
                                onClick={handleDownloadSpeechToText}
                                disabled={speechToTextIsCloud || downloadingSpeechToText || speechToTextStatus?.downloaded}
                                aria-label="Download local speech model"
                                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent-light outline-none transition-colors hover:border-accent/35 hover:bg-accent/15 focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:border-white/[0.08] disabled:bg-white/[0.03] disabled:text-neutral-500"
                                title={speechToTextStatus?.downloaded ? 'Already downloaded' : 'Download local model'}
                              >
                                {downloadingSpeechToText ? <Loader2 size={12} className="animate-spin" /> : <Download size={12} />}
                              </button>
                              <button
                                type="button"
                                onClick={handleOffloadSpeechToText}
                                disabled={speechToTextIsCloud || offloadingSpeechToText || !speechToTextStatus?.loaded}
                                aria-label="Offload local speech model"
                                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-white/[0.08] bg-white/[0.03] text-neutral-300 outline-none transition-colors hover:border-white/[0.16] hover:bg-white/[0.06] focus:ring-2 focus:ring-accent/20 disabled:cursor-not-allowed disabled:text-neutral-600 disabled:opacity-60"
                                title="Offload local model from memory"
                              >
                                {offloadingSpeechToText ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                              </button>
                              <button
                                type="button"
                                onClick={handleDeleteSpeechToText}
                                disabled={speechToTextIsCloud || deletingSpeechToText || !speechToTextStatus?.downloaded}
                                aria-label="Delete local speech model"
                                className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-red-400/20 bg-red-500/10 text-red-200 outline-none transition-colors hover:border-red-400/35 hover:bg-red-500/15 focus:ring-2 focus:ring-red-400/20 disabled:cursor-not-allowed disabled:border-white/[0.08] disabled:bg-white/[0.03] disabled:text-neutral-600 disabled:opacity-60"
                                title="Delete local model file"
                              >
                                {deletingSpeechToText ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                              </button>
                            </div>
                          </div>
                          {speechToTextError && (
                            <p className="mt-2 text-xs leading-relaxed text-red-300">{speechToTextError}</p>
                          )}
                        </div>

                        <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                          <div className="mb-3 flex items-center justify-between gap-3">
                            <p className="text-xs font-semibold text-neutral-200">Runtime limits</p>
                            <span className="status-pill border-white/[0.08] bg-white/[0.03] text-neutral-400">per turn</span>
                          </div>
                          <div className="divide-y divide-white/[0.05] rounded-lg border border-white/[0.06] overflow-hidden">
                            <label className="flex cursor-text items-center justify-between gap-4 bg-white/[0.025] px-3 py-2 transition-colors focus-within:bg-white/[0.04]">
                              <span className="text-xs text-neutral-500">Tools</span>
                              <div className="flex items-baseline gap-1.5">
                                <input
                                  type="number"
                                  aria-label="Tool iteration limit"
                                  min={1}
                                  max={500}
                                  value={draft.llm.max_iterations_per_turn}
                                  onChange={(e) => updateLlmNumber('max_iterations_per_turn', e.target.value, 1, 500)}
                                  className="runtime-limit-input w-14 bg-transparent text-right text-sm font-semibold text-neutral-100 outline-none"
                                />
                                <span className="text-[10px] text-neutral-500">calls</span>
                              </div>
                            </label>
                            <label className="flex cursor-text items-center justify-between gap-4 bg-white/[0.025] px-3 py-2 transition-colors focus-within:bg-white/[0.04]">
                              <span className="text-xs text-neutral-500">Turn</span>
                              <div className="flex items-baseline gap-1.5">
                                <input
                                  type="number"
                                  aria-label="Turn timeout seconds"
                                  min={30}
                                  max={14400}
                                  value={draft.llm.max_turn_seconds}
                                  onChange={(e) => updateLlmNumber('max_turn_seconds', e.target.value, 30, 14400)}
                                  className="runtime-limit-input w-14 bg-transparent text-right text-sm font-semibold text-neutral-100 outline-none"
                                />
                                <span className="text-[10px] text-neutral-500">sec</span>
                              </div>
                            </label>
                            <label className="flex cursor-text items-center justify-between gap-4 bg-white/[0.025] px-3 py-2 transition-colors focus-within:bg-white/[0.04]">
                              <span className="text-xs text-neutral-500">LLM call</span>
                              <div className="flex items-baseline gap-1.5">
                                <input
                                  type="number"
                                  aria-label="LLM call timeout seconds"
                                  min={30}
                                  max={1800}
                                  value={draft.llm.max_llm_call_seconds}
                                  onChange={(e) => updateLlmNumber('max_llm_call_seconds', e.target.value, 30, 1800)}
                                  className="runtime-limit-input w-14 bg-transparent text-right text-sm font-semibold text-neutral-100 outline-none"
                                />
                                <span className="text-[10px] text-neutral-500">sec</span>
                              </div>
                            </label>
                          </div>
                        </div>
                      </div>
                    </div>
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'apiKeys' && (
              <SettingsPanel
                icon={KeyRound}
                title="Connections"
                description="Choose a connection portal and configure the required tokens for that integration."
              >
                <ConnectionPortalPanel
                  selectedPortal={activeConnectionPortal}
                  values={connectionSecretValues}
                  statuses={connectionSecretStatuses}
                  showSecrets={showKey}
                  onPortalChange={setActiveConnectionPortal}
                  onSecretChange={updateConnectionSecret}
                  onToggleSecrets={() => setShowKey((value) => !value)}
                />
                {activeConnectionPortal === 'telegram' && (
                  <div className="mt-4 rounded-xl border border-white/[0.07] bg-black/10 p-4">
                    <div className="mb-3 flex items-center justify-between gap-3">
                      <p className="text-xs font-semibold text-neutral-200">Telegram allowlist</p>
                      <span className={`status-pill border-white/[0.08] ${telegramAllowlistConfigured ? 'bg-emerald-400/10 text-emerald-200' : 'bg-amber-400/10 text-amber-200'}`}>
                        {telegramAllowlistConfigured ? 'configured' : 'required'}
                      </span>
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field label="Allowed user IDs">
                        <input
                          value={telegramAllowedUserIds}
                          onChange={(e) => {
                            setTelegramAllowedUserIds(e.target.value)
                            setDirtyTelegramAllowlist(true)
                          }}
                          placeholder="123456789, 987654321"
                          className="control w-full rounded-xl px-3 py-2 text-sm"
                        />
                      </Field>
                      <Field label="Allowed chat IDs">
                        <input
                          value={telegramAllowedChatIds}
                          onChange={(e) => {
                            setTelegramAllowedChatIds(e.target.value)
                            setDirtyTelegramAllowlist(true)
                          }}
                          placeholder="-1001234567890"
                          className="control w-full rounded-xl px-3 py-2 text-sm"
                        />
                      </Field>
                    </div>
                    <p className="mt-3 text-xs leading-relaxed text-neutral-500">
                      User ID is your Telegram account's numeric from.id. Group access uses the chat.id, often a negative -100... value.
                    </p>
                  </div>
                )}
              </SettingsPanel>
            )}

            {activeTab === 'identity' && (
              <SettingsPanel
                icon={UserRound}
                title="Identity"
                description="Configure how the agent presents itself, how it addresses you, and the communication style it should use."
              >
                <div className="grid gap-4 lg:grid-cols-[1fr_0.85fr]">
                  <div className="space-y-4">
                    <div className="grid gap-4 sm:grid-cols-2">
                      <Field label="Agent nickname">
                        <input
                          value={draft.identity.agent_name}
                          maxLength={80}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, agent_name: e.target.value },
                          }))}
                          placeholder={DEFAULT_AGENT_NAME}
                          className="control w-full rounded-xl px-3 py-2 text-sm"
                        />
                      </Field>

                      <Field label="Call me">
                        <input
                          value={draft.identity.user_name}
                          maxLength={80}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, user_name: e.target.value },
                          }))}
                          placeholder="Your preferred name"
                          className="control w-full rounded-xl px-3 py-2 text-sm"
                        />
                      </Field>
                    </div>

                    <Field label="User identity">
                      <textarea
                        value={draft.identity.user_identity}
                        maxLength={1000}
                        rows={5}
                        onChange={(e) => updateDraft((current) => ({
                          ...current,
                          identity: { ...current.identity, user_identity: e.target.value },
                        }))}
                        placeholder="Role, work context, preferences, or background the agent should remember."
                        className="control w-full resize-none rounded-xl px-3 py-2 text-sm leading-relaxed"
                      />
                    </Field>

                    <Field label="Communication style">
                      <textarea
                        value={draft.identity.communication_style}
                        maxLength={1000}
                        rows={5}
                        onChange={(e) => updateDraft((current) => ({
                          ...current,
                          identity: { ...current.identity, communication_style: e.target.value },
                        }))}
                        placeholder="Tone, language, level of detail, formatting, and how direct the agent should be."
                        className="control w-full resize-none rounded-xl px-3 py-2 text-sm leading-relaxed"
                      />
                    </Field>

                    <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <p className="text-xs font-semibold text-neutral-200">Custom instructions</p>
                          <p className="mt-1 break-all text-[10px] text-neutral-600">{customInstructionsPath || 'AGENTS.md'}</p>
                        </div>
                        <div className="flex items-center gap-2">
                          <button
                            type="button"
                            onClick={handleResetCustomInstructions}
                            disabled={resettingCustomInstructions || savingCustomInstructions}
                            aria-label="Restore default custom instructions"
                            className="ghost-button rounded-lg px-2.5 py-1.5 text-xs"
                          >
                            {resettingCustomInstructions ? <Loader2 size={12} className="animate-spin" /> : <Trash2 size={12} />}
                            Restore default
                          </button>
                          <button
                            type="button"
                            onClick={handleSaveCustomInstructions}
                            disabled={!customInstructionsDirty || savingCustomInstructions || resettingCustomInstructions}
                            aria-label="Apply custom instructions"
                            className="primary-button rounded-lg px-2.5 py-1.5 text-xs disabled:opacity-50"
                          >
                            {savingCustomInstructions ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
                            {customInstructionsDirty ? 'Apply' : 'Applied'}
                          </button>
                        </div>
                      </div>
                      <textarea
                        aria-label="Custom instructions"
                        value={customInstructions}
                        maxLength={20000}
                        rows={8}
                        onChange={(e) => setCustomInstructions(e.target.value)}
                        placeholder="# AGENTS.md"
                        className="control w-full resize-y rounded-xl px-3 py-2 font-mono text-xs leading-relaxed"
                      />
                      <p className="mt-2 text-[10px] leading-relaxed text-neutral-600">
                        Loaded from .monaw workspace instructions. These guide agent behavior without overriding system rules, identity, or permission checks.
                      </p>
                      {customInstructionsError && (
                        <p className="mt-2 text-xs leading-relaxed text-red-300">{customInstructionsError}</p>
                      )}
                    </div>
                  </div>

                  <div className="panel-muted rounded-xl p-4">
                    <p className="section-label">Runtime profile</p>
                    <div className="mt-4 space-y-3 text-xs">
                      <PathStat label="Agent" value={resolveAgentName(draft.identity.agent_name)} />
                      <PathStat label="User" value={draft.identity.user_name.trim() || '-'} />
                      <PathStat label="User context" value={draft.identity.user_identity.trim() || '-'} />
                      <PathStat label="Style" value={draft.identity.communication_style.trim() || '-'} />
                      <PathStat label="Instructions" value={customInstructionsPath || '-'} />
                    </div>
                    <div className="mt-4">
                      <Notice>
                        Identity settings adjust the Monaw nickname, user context, and tone. Tool rules, permission checks, and safety constraints still take priority.
                      </Notice>
                    </div>
                  </div>
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'skills' && (
              <SettingsPanel
                icon={Wrench}
                title="Skills"
                description="Enable capabilities for the agent runtime. Click a skill to see details."
              >
                <div className="grid items-start gap-4 lg:grid-cols-2">
                  {(['recommended', 'optional'] as const).map((tier) => (
                    <div key={tier} className="panel-muted rounded-xl p-3">
                      <div className="mb-3 flex items-center justify-between gap-2">
                        <p className="text-xs font-semibold text-neutral-200">{SKILL_GROUP_COPY[tier].title}</p>
                        <span className="status-pill border-white/[0.08] bg-white/[0.03] text-neutral-400">
                          {skillGroups[tier].length}
                        </span>
                      </div>
                      <div className="space-y-1">
                        {skillGroups[tier].map((skill) => {
                          const effectiveEnabled = skill.available && !!draft.tools.skills[skill.name]
                          const health = getSkillHealth(skill)
                          const isExpanded = expandedSkill === skill.name
                          return (
                            <div key={skill.name} className="overflow-hidden rounded-lg border border-white/[0.07] bg-black/10">
                              <div className="flex items-center gap-2.5 px-3 py-2 text-xs">
                                <input
                                  type="checkbox"
                                  checked={effectiveEnabled}
                                  disabled={!skill.available || skill.always}
                                  onChange={(e) => updateDraft((current) => ({
                                    ...current,
                                    tools: {
                                      ...current.tools,
                                      skills: { ...current.tools.skills, [skill.name]: e.target.checked },
                                    },
                                  }))}
                                  className="accent-accent h-3.5 w-3.5 shrink-0 cursor-pointer"
                                />
                                <button
                                  type="button"
                                  onClick={() => setExpandedSkill(isExpanded ? null : skill.name)}
                                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                >
                                  <span className="truncate font-medium text-neutral-200">{skill.name}</span>
                                  <span className={`ml-auto shrink-0 status-pill ${skillHealthClass(skill)}`}>{health}</span>
                                  <ChevronDown
                                    size={11}
                                    className={`shrink-0 text-neutral-600 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
                                  />
                                </button>
                              </div>
                              {isExpanded && (
                                <div className="border-t border-white/[0.06] px-3 pb-3 pt-2 text-xs leading-relaxed text-neutral-500">
                                  <p>{skill.description || 'No description.'}</p>
                                  {SKILL_GUIDANCE[skill.name] && (
                                    <p className="mt-1 text-neutral-600">{SKILL_GUIDANCE[skill.name]}</p>
                                  )}
                                  {!skill.available && (
                                    <p className="mt-1 text-amber-300">
                                      {formatUnavailableReason(skill.unavailable_reason)}
                                      {skill.load_error ? `: ${skill.load_error}` : ''}
                                    </p>
                                  )}
                                </div>
                              )}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  ))}
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'memory' && (
              <SettingsPanel
                icon={Brain}
                title="Memory"
                description="Review durable user preferences, behavior, workflow context, and memory retrieval settings."
              >
                <MemorySettingsPanel draft={draft} updateDraft={updateDraft} />
              </SettingsPanel>
            )}

            {activeTab === 'browser' && (
              <SettingsPanel
                icon={Globe2}
                title="Browser"
                description="Control Browser Use launch mode, Chrome fallback, storage paths, and diagnostics."
                action={
                  <button
                    type="button"
                    onClick={handleResetBrowserSession}
                    disabled={resettingBrowserSession || !browserSkillEnabled}
                    className="ghost-button rounded-xl px-3 py-2 text-xs font-medium disabled:opacity-50"
                  >
                    <RotateCcw size={13} />
                    {resettingBrowserSession ? 'Resetting' : 'Reset Session'}
                  </button>
                }
              >
                <div className="space-y-3">
                  <RuntimeSummary
                    enabled={browserSkillEnabled}
                    available={browserSkill?.available ?? true}
                    browserDiagnostics={browserDiagnostics}
                  />

                  {browserSkill?.available === false && (
                    <Notice tone="amber">
                      browser-use package not installed in the backend Python environment.
                    </Notice>
                  )}

                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label="Launch mode">
                      <Dropdown<AgentSettings['browser']['mode']>
                        value={draft.browser.mode}
                        options={[
                          { value: 'auto', label: 'Auto (managed first)' },
                          { value: 'managed', label: 'Managed only' },
                          { value: 'system', label: 'System CDP only' },
                        ]}
                        onChange={(mode) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, mode },
                        }))}
                      />
                    </Field>
                    <Field label="System connection">
                      <Dropdown<'auto' | 'attach'>
                        value={draft.browser.system_connection_strategy === 'launch' ? 'auto' : draft.browser.system_connection_strategy}
                        options={[
                          { value: 'auto', label: 'Auto (attach first)' },
                          { value: 'attach', label: 'Attach only' },
                        ]}
                        onChange={(system_connection_strategy) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, system_connection_strategy },
                        }))}
                      />
                    </Field>
                    <Field label="CDP URL">
                      <input
                        value={draft.browser.system_cdp_url}
                        onChange={(e) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, system_cdp_url: e.target.value },
                        }))}
                        placeholder="http://127.0.0.1:9222"
                        className="control h-9 w-full rounded-lg px-3 text-xs"
                      />
                    </Field>
                  </div>

                  <div className="grid gap-3 sm:grid-cols-[1fr_1fr]">
                    <Field label="Chrome profile">
                      <Dropdown
                        value={draft.browser.system_profile_directory || ''}
                        options={[
                          { value: '', label: 'Auto-detect' },
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
                    </Field>
                    <Field label="Allowed domains">
                      <input
                        value={allowedDomainsInput}
                        onChange={(e) => updateDraft((current) => ({
                          ...current,
                          browser: {
                            ...current.browser,
                            allowed_domains: e.target.value.split(',').map((item) => item.trim()).filter(Boolean),
                          },
                        }))}
                        placeholder="Comma-separated domains"
                        className="control h-9 w-full rounded-lg px-3 text-xs"
                      />
                    </Field>
                  </div>

                  <div className="grid gap-1.5 text-xs sm:grid-cols-3">
                    <ToggleRow
                      label="Allow system fallback"
                      checked={draft.browser.enable_system_fallback}
                      onChange={(checked) => updateDraft((current) => ({
                        ...current,
                        browser: { ...current.browser, enable_system_fallback: checked },
                      }))}
                    />
                    <ToggleRow
                      label="Headless"
                      checked={draft.browser.headless}
                      onChange={(checked) => updateDraft((current) => ({
                        ...current,
                        browser: { ...current.browser, headless: checked },
                      }))}
                    />
                    <ToggleRow
                      label="Keep session alive"
                      checked={draft.browser.keep_alive}
                      onChange={(checked) => updateDraft((current) => ({
                        ...current,
                        browser: { ...current.browser, keep_alive: checked },
                      }))}
                    />
                  </div>

                  <div className="panel-muted space-y-3 rounded-xl p-3">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p className="text-xs font-semibold text-neutral-200">Output workspace</p>
                        <p className="mt-1 text-xs text-neutral-600">
                          Screenshots and downloads are saved here. Runtime profiles and traces stay in hidden app storage.
                        </p>
                      </div>
                      {window.electronAPI?.selectDirectory && (
                        <button
                          type="button"
                          onClick={setOutputRoot}
                          className="ghost-button rounded-lg px-2.5 py-1.5 text-xs"
                        >
                          <Folder size={12} />
                          Choose root
                        </button>
                      )}
                    </div>
                    <div className="grid gap-3 lg:grid-cols-2">
                      <OutputFolderField
                        label="Screenshots"
                        value={draft.browser.screenshots_dir}
                        onChange={(screenshots_dir) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, screenshots_dir },
                        }))}
                        onBrowse={window.electronAPI?.selectDirectory ? () => chooseBrowserFolder('screenshots_dir') : undefined}
                      />
                      <OutputFolderField
                        label="Downloads"
                        value={draft.browser.downloads_dir}
                        onChange={(downloads_dir) => updateDraft((current) => ({
                          ...current,
                          browser: { ...current.browser, downloads_dir },
                        }))}
                        onBrowse={window.electronAPI?.selectDirectory ? () => chooseBrowserFolder('downloads_dir') : undefined}
                      />
                    </div>
                    <div className="grid gap-1.5 lg:grid-cols-2">
                      <PathStat label="Managed profile" value={draft.browser.managed_profile_dir} />
                      <PathStat label="Traces" value={draft.browser.traces_dir} />
                    </div>
                  </div>

                  <div className="rounded-lg border border-white/[0.07] bg-black/10 text-xs">
                    <button
                      type="button"
                      onClick={() => setDiagOpen((v) => !v)}
                      className="flex w-full items-center justify-between px-3 py-2 text-neutral-400 hover:text-neutral-200"
                    >
                      <span className="font-medium">Diagnostics</span>
                      <ChevronDown size={12} className={`transition-transform ${diagOpen ? 'rotate-180' : ''}`} />
                    </button>
                    {diagOpen && (
                      <div className="border-t border-white/[0.06]">
                        <BrowserDiagnosticsCard diagnostics={browserDiagnostics} draft={draft} />
                      </div>
                    )}
                  </div>
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'mcp' && (
              <SettingsPanel
                icon={Server}
                title="MCP"
                description="Add, diagnose, and reconnect external tool servers."
                action={
                  <div className="flex flex-wrap items-center gap-2">
                    <Dropdown<MCPServerTemplateKey>
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
                      className="primary-button rounded-xl px-3 py-2 text-xs font-medium"
                    >
                      <Plus size={13} />
                      Add Server
                    </button>
                  </div>
                }
              >
                <div className="space-y-4">
                  <div className="panel-muted flex items-center justify-between gap-3 rounded-xl px-3 py-2.5">
                    <p className="text-xs font-semibold text-neutral-200">MCP bridge</p>
                    <ToggleRow
                      label={mcpFeatureEnabled ? 'Enabled' : 'Disabled'}
                      checked={mcpFeatureEnabled}
                      onChange={(checked) => updateDraft((current) => ({
                        ...current,
                        mcp: { ...current.mcp, enabled: checked },
                      }))}
                    />
                  </div>

                  {!mcpFeatureAvailable && (
                    <Notice tone="amber">
                      The MCP bridge is built in, but the backend Python environment is missing the mcp package{mcpFeatureUnavailableReason ? ` (${mcpFeatureUnavailableReason})` : ''}.
                    </Notice>
                  )}
                  {duplicateMcpNames.length > 0 && (
                    <Notice tone="red">
                      MCP server names must be unique: {duplicateMcpNames.join(', ')}
                    </Notice>
                  )}

                  {draft.mcp.servers.length === 0 ? (
                    <div className="panel-muted rounded-xl px-4 py-8 text-center text-sm text-neutral-500">
                      No MCP servers configured.
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {draft.mcp.servers.map((server, index) => {
                        const diagnostic = server.name ? mcpDiagnostics[server.name] : undefined
                        const statusText = !mcpFeatureEnabled
                          ? 'MCP feature disabled'
                          : diagnostic?.feature_available === false
                            ? 'MCP backend dependency missing'
                          : diagnostic
                            ? diagnostic.connected
                              ? `Connected - ${diagnostic.tool_count} tools`
                              : diagnostic.last_error || 'Disconnected'
                            : 'Waiting for save or reconnect'

                        return (
                          <McpServerCard
                            key={`${server.name || 'mcp-server'}-${index}`}
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
                      })}
                    </div>
                  )}

                  <p className="text-xs leading-relaxed text-neutral-600">
                    MCP server environment variables and HTTP headers are saved in plaintext in the backend settings file.
                  </p>
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'observability' && (
              <SettingsPanel
                icon={Activity}
                title="Observability"
                description="Inspect structured traces, token usage, local errors, and replay diagnosis."
              >
                <ObservabilityPanel />
              </SettingsPanel>
            )}

            {activeTab === 'permissions' && (
              <SettingsPanel
                icon={Shield}
                title="Permissions"
                description="Tune approval prompts, high-risk action gates, path overrides, and app overrides."
              >
                <div className="space-y-4">
                  <div className="panel-muted rounded-xl p-3">
                    <p className="mb-2 text-xs font-semibold text-neutral-200">Approval profile</p>
                    <div className="grid gap-1.5 sm:grid-cols-3">
                      {(['default', 'full_access', 'custom'] as const).map((mode) => (
                        <button
                          key={mode}
                          type="button"
                          onClick={() => updatePermissionMode(mode)}
                          className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors ${
                            draft.permissions.mode === mode
                              ? 'border-accent bg-accent text-white'
                              : 'border-white/[0.08] bg-white/[0.03] text-neutral-400 hover:text-neutral-100'
                          }`}
                        >
                          {PERMISSION_MODE_LABEL[mode]}
                        </button>
                      ))}
                    </div>
                    <p className="mt-2 text-[11px] leading-relaxed text-neutral-500">
                      {PERMISSION_MODE_HELP[draft.permissions.mode]}
                    </p>
                  </div>

                  <div className="grid gap-3 lg:grid-cols-2">
                    <div className="panel-muted rounded-xl p-3">
                      <p className="mb-2 text-xs font-semibold text-neutral-200">Confirmation prompts</p>
                      <div className="space-y-1">
                        {(Object.entries(displayedPermissions.confirmations) as Array<[keyof AgentSettings['permissions']['confirmations'], boolean]>).map(([key, value]) => (
                          <ToggleRow
                            key={key}
                            label={`Confirm ${key.replace('_', ' ')}`}
                            checked={value}
                            onChange={(checked) => updateCustomPermissionDraft((current) => ({
                              ...current,
                              permissions: {
                                ...current.permissions,
                                confirmations: {
                                  ...current.permissions.confirmations,
                                  [key]: checked,
                                },
                              },
                            }))}
                          />
                        ))}
                      </div>
                    </div>

                    <div className="panel-muted rounded-xl p-3">
                      <p className="mb-2 text-xs font-semibold text-neutral-200">Risk gates</p>
                      <div className="space-y-1">
                        <ToggleRow
                          label="Allow delete actions"
                          checked={displayedPermissions.allow_delete}
                          onChange={(checked) => updateCustomPermissionDraft((current) => ({
                            ...current,
                            permissions: { ...current.permissions, allow_delete: checked },
                          }))}
                        />
                        <ToggleRow
                          label="Dangerous actions require confirm"
                          checked={displayedPermissions.dangerous_actions_require_confirm}
                          onChange={(checked) => updateCustomPermissionDraft((current) => ({
                            ...current,
                            permissions: { ...current.permissions, dangerous_actions_require_confirm: checked },
                          }))}
                        />
                        <ToggleRow
                          label="Allow screen fallback"
                          checked={displayedPermissions.allow_screen_fallback}
                          onChange={(checked) => updateCustomPermissionDraft((current) => ({
                            ...current,
                            permissions: { ...current.permissions, allow_screen_fallback: checked },
                          }))}
                        />
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-3 xl:grid-cols-2">
                    <PathOverrides
                      draft={draft}
                      updateDraft={updateCustomPermissionDraft}
                      blockedRootInput={blockedRootInput}
                      setBlockedRootInput={setBlockedRootInput}
                    />
                    <AppOverrides draft={draft} updateDraft={updateCustomPermissionDraft} />
                  </div>
                </div>
              </SettingsPanel>
            )}

            {activeTab === 'sandbox' && (
              <SettingsPanel
                icon={Container}
                title="Sandbox"
                description="Control exec isolation, environment scrubbing, network policy, and backend selection."
              >
                <div className="space-y-4">
                  {/* Policy toggle */}
                  <div className="panel-muted rounded-xl p-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <div>
                        <p className="text-xs font-semibold text-neutral-200">Sandbox policy</p>
                        <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
                          Local direct is compatibility only. Local restricted is advisory. Docker is the strong backend when available.
                        </p>
                      </div>
                      <ToggleRow
                        label={draft.sandbox.enabled ? 'Enabled' : 'Disabled'}
                        checked={draft.sandbox.enabled}
                        onChange={(checked) => updateDraft((current) => ({
                          ...current,
                          sandbox: { ...current.sandbox, enabled: checked },
                        }))}
                      />
                    </div>
                  </div>

                  {/* Execution policy — compact inline rows */}
                  <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                    <p className="mb-3 text-xs font-semibold text-neutral-200">Execution policy</p>
                    <div className="divide-y divide-white/[0.05] overflow-hidden rounded-lg border border-white/[0.06]">
                      <div className="flex items-center justify-between gap-4 bg-white/[0.025] px-3 py-2">
                        <span className="text-xs text-neutral-500">Mode</span>
                        <Dropdown
                          size="sm"
                          className="w-40"
                          value={draft.sandbox.mode}
                          options={[
                            { value: 'disabled', label: 'Disabled' },
                            { value: 'auto', label: 'Auto' },
                            { value: 'enforce', label: 'Enforce' },
                            { value: 'docker', label: 'Docker' },
                            { value: 'local_restricted', label: 'Local restricted' },
                          ]}
                          onChange={(mode) => updateDraft((current) => ({
                            ...current,
                            sandbox: { ...current.sandbox, mode },
                          }))}
                        />
                      </div>
                      <div className="flex items-center justify-between gap-4 bg-white/[0.025] px-3 py-2">
                        <span className="text-xs text-neutral-500">Network</span>
                        <Dropdown
                          size="sm"
                          className="w-40"
                          value={draft.sandbox.network.default}
                          options={[
                            { value: 'deny', label: 'Deny' },
                            { value: 'allow_with_approval', label: 'Allow with approval' },
                            { value: 'allow', label: 'Allow' },
                          ]}
                          onChange={(defaultNetwork) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              network: { ...current.sandbox.network, default: defaultNetwork },
                            },
                          }))}
                        />
                      </div>
                      <div className="flex items-center justify-between gap-4 bg-white/[0.025] px-3 py-2">
                        <span className="text-xs text-neutral-500">Write strategy</span>
                        <Dropdown
                          size="sm"
                          className="w-40"
                          value={draft.sandbox.default_write_strategy}
                          options={[
                            { value: 'discard', label: 'Discard' },
                            { value: 'copy_out', label: 'Copy out' },
                            { value: 'direct_rw', label: 'Direct RW' },
                          ]}
                          onChange={(strategy) => updateDraft((current) => ({
                            ...current,
                            sandbox: { ...current.sandbox, default_write_strategy: strategy },
                          }))}
                        />
                      </div>
                    </div>
                  </div>

                  {/* Backends + Docker side by side */}
                  <div className="grid gap-3 lg:grid-cols-2">
                    <div className="panel-muted rounded-xl p-3">
                      <p className="mb-2 text-xs font-semibold text-neutral-200">Backends</p>
                      <div className="space-y-2 text-xs">
                        {['docker', 'local_restricted', 'wsl'].map((backend) => {
                          const status = sandboxStatus?.backends?.[backend]
                          return (
                            <div key={backend} className="flex items-center justify-between gap-3 rounded-lg border border-white/[0.06] bg-black/10 px-3 py-2">
                              <div>
                                <p className="font-medium capitalize text-neutral-200">{backend.replace('_', ' ')}</p>
                                <p className="text-[11px] text-neutral-500">
                                  {status?.available ? `${status.security_label} isolation` : status?.reason || 'Waiting for status'}
                                </p>
                              </div>
                              <span className={`status-pill border-white/[0.08] ${status?.available ? 'bg-emerald-500/15 text-emerald-200' : 'bg-amber-500/15 text-amber-200'}`}>
                                {status?.available ? 'available' : 'unavailable'}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    </div>

                    <div className="panel-muted rounded-xl p-3">
                      <p className="mb-2 text-xs font-semibold text-neutral-200">Docker defaults</p>
                      <div className="space-y-2">
                        <input
                          value={draft.sandbox.docker.image}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              docker: { ...current.sandbox.docker, image: e.target.value },
                            },
                          }))}
                          placeholder="Docker image"
                          className="control w-full rounded-xl px-3 py-2 text-sm"
                        />
                        <ToggleRow
                          label="Enable Docker backend"
                          checked={draft.sandbox.docker.enabled}
                          onChange={(checked) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              docker: { ...current.sandbox.docker, enabled: checked },
                            },
                          }))}
                        />
                        <ToggleRow
                          label="Read-only root filesystem"
                          checked={draft.sandbox.docker.read_only_root}
                          onChange={(checked) => updateDraft((current) => ({
                            ...current,
                            sandbox: {
                              ...current.sandbox,
                              docker: { ...current.sandbox.docker, read_only_root: checked },
                            },
                          }))}
                        />
                      </div>
                    </div>
                  </div>

                  {/* Resources — compact inline rows */}
                  <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
                    <p className="mb-3 text-xs font-semibold text-neutral-200">Resources</p>
                    <div className="divide-y divide-white/[0.05] overflow-hidden rounded-lg border border-white/[0.06]">
                      {([
                        { key: 'timeout_seconds' as const, label: 'Timeout', unit: 'sec' },
                        { key: 'memory_mb' as const, label: 'Memory', unit: 'MB' },
                        { key: 'cpus' as const, label: 'CPUs', unit: null },
                        { key: 'pids' as const, label: 'PIDs', unit: null },
                      ]).map(({ key, label, unit }) => (
                        <label key={key} className="flex cursor-text items-center justify-between gap-4 bg-white/[0.025] px-3 py-2 transition-colors focus-within:bg-white/[0.04]">
                          <span className="text-xs text-neutral-500">{label}</span>
                          <div className="flex items-baseline gap-1.5">
                            <input
                              type="number"
                              value={draft.sandbox.resources[key]}
                              onChange={(e) => updateDraft((current) => ({
                                ...current,
                                sandbox: {
                                  ...current.sandbox,
                                  resources: {
                                    ...current.sandbox.resources,
                                    [key]: Number(e.target.value),
                                  },
                                },
                              }))}
                              className="runtime-limit-input w-16 bg-transparent text-right text-sm font-semibold text-neutral-100 outline-none"
                            />
                            {unit && <span className="text-[10px] text-neutral-500">{unit}</span>}
                          </div>
                        </label>
                      ))}
                    </div>
                  </div>

                  {draft.sandbox.mode === 'enforce' && !sandboxStatus?.backends?.docker?.available && (
                    <Notice tone="amber">
                      Enforce mode requires Docker. Commands will block until a strong backend is available.
                    </Notice>
                  )}
                </div>
              </SettingsPanel>
            )}

            <div className="h-[5px] shrink-0" aria-hidden="true" />
          </div>

          <footer className="flex items-center justify-between gap-3 border-t border-white/[0.07] px-5 py-4">
            <div className="flex items-center gap-2 text-xs text-neutral-500">
              {duplicateMcpNames.length > 0 ? (
                <>
                  <AlertCircle size={14} className="text-red-300" />
                  Fix duplicate MCP names before saving.
                </>
              ) : saved ? (
                <>
                  <CheckCircle2 size={14} className="text-emerald-300" />
                  Settings saved.
                </>
              ) : (
                'Changes apply after Save.'
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                className="ghost-button rounded-xl px-4 py-2 text-sm"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSave}
                disabled={saving || duplicateMcpNames.length > 0}
                className="primary-button rounded-xl px-4 py-2 text-sm font-medium"
              >
                <Save size={14} />
                {saved ? 'Saved' : saving ? 'Saving' : 'Save'}
              </button>
            </div>
          </footer>
        </div>
      </div>
    </div>
  )
}

function SettingsPanel({
  icon: Icon,
  title,
  description,
  action,
  children,
}: {
  icon: LucideIcon
  title: string
  description: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex min-h-0 flex-col">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-accent/20 bg-accent/10 text-accent-light">
            <Icon size={15} />
          </div>
          <div>
            <h3 className="text-sm font-semibold tracking-tight text-neutral-100">{title}</h3>
            <p className="mt-0.5 max-w-2xl text-xs leading-relaxed text-neutral-500">{description}</p>
          </div>
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="section-label">{label}</span>
      {children}
    </label>
  )
}

function LimitInput({
  label,
  inputLabel,
  unit,
  min,
  max,
  value,
  onChange,
}: {
  label: string
  inputLabel: string
  unit: string
  min: number
  max: number
  value: number
  onChange: (value: string) => void
}) {
  return (
    <label className="rounded-lg border border-white/[0.07] bg-white/[0.025] px-3 py-2 focus-within:border-accent/60 focus-within:ring-2 focus-within:ring-accent/10">
      <span className="block text-[10px] font-semibold uppercase tracking-[0.16em] text-neutral-500">{label}</span>
      <span className="mt-1 flex items-baseline gap-2">
        <input
          type="number"
          aria-label={inputLabel}
          min={min}
          max={max}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="runtime-limit-input min-w-0 flex-1 bg-transparent text-lg font-semibold text-neutral-100 outline-none"
        />
        <span className="shrink-0 text-xs font-medium text-neutral-500">{unit}</span>
      </span>
    </label>
  )
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-3 rounded-xl border border-white/[0.07] bg-black/10 px-3 py-2 text-xs text-neutral-300 transition-colors hover:border-white/[0.12] hover:bg-white/[0.04] hover:text-neutral-100">
      <span>{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-accent h-3.5 w-3.5 cursor-pointer"
      />
    </label>
  )
}

function SettingStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span>{label}</span>
      <span className="font-medium text-neutral-200">{value}</span>
    </div>
  )
}

function Notice({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'amber' | 'red' }) {
  const toneClass = {
    neutral: 'border-white/[0.08] bg-white/[0.025] text-neutral-500',
    amber: 'border-amber-400/25 bg-amber-400/10 text-amber-200',
    red: 'border-red-400/25 bg-red-400/10 text-red-200',
  }[tone]

  return (
    <div className={`rounded-xl border px-4 py-3 text-xs leading-relaxed ${toneClass}`}>
      {children}
    </div>
  )
}

function PathStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-white/[0.07] bg-black/10 px-3 py-2 text-xs">
      <span className="font-medium text-neutral-400">{label}: </span>
      <span className="break-all text-neutral-600">{value || '-'}</span>
    </div>
  )
}

function OutputFolderField({
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
    <div className="space-y-1.5">
      <span className="section-label">{label}</span>
      <div className="flex gap-2">
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Folder path"
          className="control h-9 min-w-0 flex-1 rounded-lg px-3 text-xs"
        />
        {onBrowse && (
          <button
            type="button"
            onClick={onBrowse}
            className="ghost-button h-9 rounded-lg px-2.5 text-xs"
          >
            <Folder size={12} />
            Browse
          </button>
        )}
      </div>
    </div>
  )
}

function RuntimeSummary({
  enabled,
  available,
  browserDiagnostics,
}: {
  enabled: boolean
  available: boolean
  browserDiagnostics: BrowserUseDiagnostics | null
}) {
  const status = !available
    ? 'Unavailable'
    : enabled
      ? browserDiagnostics?.session_active && browserDiagnostics.current_mode
        ? `${browserDiagnostics.current_mode} active`
        : browserDiagnostics?.last_error
          ? 'Degraded'
          : 'Ready'
      : 'Disabled'

  return (
    <div className="panel-muted flex items-center divide-x divide-white/[0.07] rounded-xl text-xs">
      <div className="px-4 py-2.5">
        <p className="section-label">Skill</p>
        <p className="mt-1 font-semibold text-neutral-100">{enabled ? 'Enabled' : 'Disabled'}</p>
      </div>
      <div className="px-4 py-2.5">
        <p className="section-label">Session</p>
        <p className="mt-1 font-semibold text-neutral-100">{status}</p>
      </div>
      <div className="px-4 py-2.5">
        <p className="section-label">Tabs</p>
        <p className="mt-1 font-semibold text-neutral-100">{browserDiagnostics?.tab_count ?? 0}</p>
      </div>
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
    <div className="grid gap-x-4 gap-y-1 px-3 py-2 text-xs text-neutral-500 sm:grid-cols-2">
      <p className="col-span-full font-medium text-neutral-300">
        {diagnostics?.session_active
          ? `Session active — ${diagnostics.current_mode || 'unknown'} mode`
          : 'No active browser session'}
      </p>
      {diagnostics?.current_mode === 'system' && diagnostics.current_system_connection && (
        <p className="break-all">Connection: {diagnostics.current_system_connection}</p>
      )}
      <p className="break-all">Strategy: {diagnostics?.system_connection_strategy ?? draft.browser.system_connection_strategy}</p>
      <p className="break-all">CDP URL: {diagnostics?.system_cdp_url || draft.browser.system_cdp_url}</p>
      {diagnostics?.chrome_executable && (
        <p className="break-all">Chrome: {diagnostics.chrome_executable}</p>
      )}
      {diagnostics?.current_page && (
        <p className="break-all">Page: {diagnostics.current_page.title || diagnostics.current_page.url}</p>
      )}
      {diagnostics?.last_error && (
        <p className="col-span-full break-all text-amber-300">Error: {diagnostics.last_error}</p>
      )}
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
  const statusDot = diagnostic?.connected
    ? 'bg-emerald-400'
    : diagnostic?.last_error
      ? 'bg-amber-400'
      : 'bg-neutral-600'

  return (
    <div className="panel-muted rounded-xl p-3">
      {/* Header: status dot + name + enabled toggle + delete */}
      <div className="mb-2.5 flex items-center gap-2">
        <span className={`h-2 w-2 shrink-0 rounded-full ${statusDot}`} />
        <input
          value={server.name}
          onChange={(e) => onUpdate((current) => ({ ...current, name: e.target.value }))}
          placeholder="Server name"
          className="control min-w-0 flex-1 rounded-lg px-2.5 py-1.5 text-sm font-medium"
        />
        <label className="inline-flex h-7 shrink-0 cursor-pointer items-center gap-1.5 rounded-lg border border-white/[0.08] bg-white/[0.03] px-2.5 text-[11px] text-neutral-300 transition-colors hover:text-neutral-100">
          <input
            type="checkbox"
            checked={server.enabled}
            onChange={(e) => onUpdate((current) => ({ ...current, enabled: e.target.checked }))}
            className="h-3 w-3 accent-[#8bcf4f]"
          />
          <span>Enabled</span>
        </label>
        <button
          type="button"
          onClick={onRemove}
          className="ghost-button h-7 w-7 shrink-0 rounded-lg hover:text-red-300"
          aria-label={`Remove MCP server ${index + 1}`}
        >
          <Trash2 size={13} />
        </button>
      </div>

      {/* Command/URL + Transport inline */}
      <div className="mb-2 flex items-center gap-2">
        {server.transport === 'streamable_http' ? (
          <input
            value={server.url}
            onChange={(e) => onUpdate((current) => ({ ...current, url: e.target.value }))}
            placeholder="MCP URL"
            className="control min-w-0 flex-1 rounded-lg px-2.5 py-1.5 font-mono text-xs"
          />
        ) : (
          <input
            value={server.command}
            onChange={(e) => onUpdate((current) => ({ ...current, command: e.target.value }))}
            placeholder="Command"
            className="control min-w-0 flex-1 rounded-lg px-2.5 py-1.5 font-mono text-xs"
          />
        )}
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-neutral-500">Transport</span>
          <Dropdown<AgentSettings['mcp']['servers'][number]['transport']>
            value={server.transport}
            options={[
              { value: 'stdio', label: 'stdio' },
              { value: 'streamable_http', label: 'http' },
            ]}
            onChange={(transport) => onUpdate((current) => ({ ...current, transport }))}
            size="sm"
            align="right"
          />
        </div>
      </div>

      {/* Working dir + description — secondary, muted */}
      <div className="mb-2.5 grid gap-2 lg:grid-cols-2">
        {server.transport === 'stdio' && (
          <input
            value={server.cwd}
            onChange={(e) => onUpdate((current) => ({ ...current, cwd: e.target.value }))}
            placeholder="Working directory (optional)"
            className="control rounded-lg px-2.5 py-1.5 text-xs text-neutral-400"
          />
        )}
        <input
          value={server.description}
          onChange={(e) => onUpdate((current) => ({ ...current, description: e.target.value }))}
          placeholder="Description (optional)"
          className={`control rounded-lg px-2.5 py-1.5 text-xs text-neutral-400 ${server.transport !== 'stdio' ? 'lg:col-span-2' : ''}`}
        />
      </div>

      {server.transport === 'stdio' && (
        <EditableList
          title="Arguments"
          emptyText="No arguments configured."
          addLabel="Add arg"
          values={server.args}
          placeholder={(itemIndex) => `Arg ${itemIndex + 1}`}
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
      )}

      {/* Footer: status + actions */}
      <div className="mt-2.5 flex flex-wrap items-center justify-between gap-3 text-xs">
        <span className={diagnostic?.connected ? 'text-emerald-300' : diagnostic?.last_error ? 'text-amber-300' : 'text-neutral-500'}>
          {statusText}
          {diagnostic?.state ? ` · ${diagnostic.state}` : ''}
          {diagnostic?.startup_phase ? ` · ${diagnostic.startup_phase}` : ''}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onToggleAdvanced}
            className="ghost-button rounded-lg px-2 py-1 text-xs"
          >
            {advancedOpen ? 'Hide Advanced' : 'Advanced'}
          </button>
          <button
            type="button"
            onClick={onReconnect}
            disabled={!server.name || reconnectingServer === server.name || !bridgeEnabled}
            className="ghost-button rounded-lg px-2 py-1 text-xs disabled:opacity-50"
          >
            {reconnectingServer === server.name ? 'Reconnecting…' : 'Reconnect'}
          </button>
        </div>
      </div>

      {advancedOpen && (
        <div className="mt-4 space-y-4 border-t border-white/[0.07] pt-4">
          <div className="grid gap-2 sm:grid-cols-2">
            <input
              type="number"
              min={1000}
              value={server.startup_timeout_ms}
              onChange={(e) => onUpdate((current) => ({ ...current, startup_timeout_ms: Number(e.target.value || 0) }))}
              placeholder="Startup timeout (ms)"
              className="control rounded-xl px-3 py-2 text-sm"
            />
            <input
              type="number"
              min={1000}
              value={server.call_timeout_ms}
              onChange={(e) => onUpdate((current) => ({ ...current, call_timeout_ms: Number(e.target.value || 0) }))}
              placeholder="Call timeout (ms)"
              className="control rounded-xl px-3 py-2 text-sm"
            />
          </div>

          <ToggleRow
            label="Reconnect on unhealthy"
            checked={server.reconnect_on_unhealthy}
            onChange={(checked) => onUpdate((current) => ({ ...current, reconnect_on_unhealthy: checked }))}
          />

          <EditableList
            title="Allow list"
            emptyText="Empty means all remote tools are exposed."
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

          {diagnostic && <McpDiagnostics diagnostic={diagnostic} />}

          {server.transport === 'stdio' ? (
            <KeyValueEditor
              title="Environment"
              addLabel="Add var"
              emptyText="No environment variables configured."
              entries={server.env}
              keyPlaceholder="Key"
              valuePlaceholder="Value"
              onAdd={() => onUpdate((current) => ({ ...current, env: { ...current.env, '': '' } }))}
              onChange={(entryIndex, key, value) => onUpdate((current) => {
                const entries = Object.entries(current.env)
                const nextEnv: Record<string, string> = {}
                entries.forEach(([entryKey, entryValue], currentIndex) => {
                  if (currentIndex === entryIndex) nextEnv[key] = value
                  else nextEnv[entryKey] = entryValue
                })
                return { ...current, env: nextEnv }
              })}
              onRemove={(entryIndex) => onUpdate((current) => {
                const entries = Object.entries(current.env)
                const nextEnv: Record<string, string> = {}
                entries.forEach(([entryKey, entryValue], currentIndex) => {
                  if (currentIndex !== entryIndex) nextEnv[entryKey] = entryValue
                })
                return { ...current, env: nextEnv }
              })}
            />
          ) : (
            <KeyValueEditor
              title="HTTP headers"
              addLabel="Add header"
              emptyText="No HTTP headers configured."
              entries={server.headers}
              keyPlaceholder="Header"
              valuePlaceholder="Value"
              onAdd={() => onUpdate((current) => ({ ...current, headers: { ...current.headers, '': '' } }))}
              onChange={(entryIndex, key, value) => onUpdate((current) => {
                const entries = Object.entries(current.headers)
                const nextHeaders: Record<string, string> = {}
                entries.forEach(([entryKey, entryValue], currentIndex) => {
                  if (currentIndex === entryIndex) nextHeaders[key] = value
                  else nextHeaders[entryKey] = entryValue
                })
                return { ...current, headers: nextHeaders }
              })}
              onRemove={(entryIndex) => onUpdate((current) => {
                const entries = Object.entries(current.headers)
                const nextHeaders: Record<string, string> = {}
                entries.forEach(([entryKey, entryValue], currentIndex) => {
                  if (currentIndex !== entryIndex) nextHeaders[entryKey] = entryValue
                })
                return { ...current, headers: nextHeaders }
              })}
            />
          )}
        </div>
      )}
    </div>
  )
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
    <div className="mt-4 space-y-2">
      <div className="flex items-center justify-between gap-3">
        <p className="section-label">{title}</p>
        <button type="button" onClick={onAdd} className="ghost-button rounded-lg px-2 py-1 text-xs">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {values.length === 0 ? (
        <p className="text-xs text-neutral-600">{emptyText}</p>
      ) : (
        values.map((value, itemIndex) => (
          <div key={`${title}-${itemIndex}`} className="flex gap-2">
            <input
              value={value}
              onChange={(e) => onChange(itemIndex, e.target.value)}
              placeholder={placeholder(itemIndex)}
              className="control flex-1 rounded-xl px-3 py-2 text-sm"
            />
            <button
              type="button"
              onClick={() => onRemove(itemIndex)}
              className="ghost-button h-10 w-10 rounded-xl hover:text-red-300"
              aria-label={`Remove ${title} item ${itemIndex + 1}`}
            >
              <Trash2 size={14} />
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
        <p className="section-label">{title}</p>
        <button type="button" onClick={onAdd} className="ghost-button rounded-lg px-2 py-1 text-xs">
          <Plus size={12} />
          {addLabel}
        </button>
      </div>
      {entryList.length === 0 ? (
        <p className="text-xs text-neutral-600">{emptyText}</p>
      ) : (
        entryList.map(([entryKey, entryValue], entryIndex) => (
          <div key={`${title}-${entryIndex}`} className="grid grid-cols-[1fr_1fr_auto] gap-2">
            <input
              value={entryKey}
              onChange={(e) => onChange(entryIndex, e.target.value, entryValue)}
              placeholder={keyPlaceholder}
              className="control min-w-0 rounded-xl px-3 py-2 text-sm"
            />
            <input
              value={entryValue}
              onChange={(e) => onChange(entryIndex, entryKey, e.target.value)}
              placeholder={valuePlaceholder}
              className="control min-w-0 rounded-xl px-3 py-2 text-sm"
            />
            <button
              type="button"
              onClick={() => onRemove(entryIndex)}
              className="ghost-button h-10 w-10 rounded-xl hover:text-red-300"
              aria-label={`Remove ${title} item ${entryIndex + 1}`}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))
      )}
    </div>
  )
}

function McpDiagnostics({ diagnostic }: { diagnostic: MCPServerDiagnostics }) {
  return (
    <div className="rounded-xl border border-white/[0.07] bg-black/15 px-3 py-3 text-xs text-neutral-500">
      <div className="grid gap-2 md:grid-cols-2">
        <p className="break-all">{diagnostic.transport === 'streamable_http' ? 'URL' : 'Command'}: {diagnostic.transport === 'streamable_http' ? diagnostic.url || '-' : [diagnostic.command, ...(diagnostic.args || [])].filter(Boolean).join(' ') || '-'}</p>
        <p className="break-all">CWD: {diagnostic.cwd || '-'}</p>
        <p className="break-all">Executable: {diagnostic.resolved_executable || '-'}</p>
        <p>PID: {diagnostic.pid ?? '-'}</p>
        <p className="break-all">Connected: {diagnostic.connected_at || '-'}</p>
        <p>Last call: {diagnostic.last_call_duration_ms ?? '-'} ms</p>
        <p>Failed calls: {diagnostic.failed_call_count}</p>
        <p>Reflected: {diagnostic.reflected_tool_names?.length ?? 0}</p>
      </div>
      {diagnostic.unhealthy_reason && (
        <p className="mt-2 break-all text-amber-300">Unhealthy: {diagnostic.unhealthy_reason}</p>
      )}
      {(diagnostic.reflected_tool_names?.length ?? 0) > 0 && (
        <p className="mt-2 break-all">Tools: {diagnostic.reflected_tool_names.join(', ')}</p>
      )}
      {diagnostic.stderr_tail && (
        <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap rounded-lg border border-white/[0.06] bg-black/30 p-2 text-[11px] text-neutral-400">
          {diagnostic.stderr_tail}
        </pre>
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
    <div className="panel-muted rounded-xl p-3">
      <div className="mb-2.5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Folder size={13} className="text-neutral-400" />
          <p className="text-xs font-semibold text-neutral-200">Path overrides</p>
        </div>
        <button
          type="button"
          onClick={() => updateDraft((current) => ({
            ...current,
            permissions: {
              ...current.permissions,
              path_rules: [...current.permissions.path_rules, emptyPathRule()],
            },
          }))}
          className="ghost-button rounded-lg px-2 py-1 text-xs"
        >
          <Plus size={12} />
          Add
        </button>
      </div>
      <div className="space-y-3">
        {draft.permissions.path_rules.map((rule, index) => (
          <div key={`${rule.path}-${index}`} className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
            <div className="flex gap-2">
              <input
                value={rule.path}
                onChange={(e) => updateDraft((current) => {
                  const pathRules = [...current.permissions.path_rules]
                  pathRules[index] = { ...pathRules[index], path: e.target.value }
                  return { ...current, permissions: { ...current.permissions, path_rules: pathRules } }
                })}
                placeholder="Folder path"
                className="control min-w-0 flex-1 rounded-xl px-3 py-2 text-sm"
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
                className="ghost-button h-10 w-10 rounded-xl hover:text-red-300"
                aria-label={`Remove path override ${index + 1}`}
              >
                <Trash2 size={14} />
              </button>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
              {([
                ['read', 'Read'],
                ['write', 'Write'],
                ['delete', 'Delete'],
                ['launch', 'Launch'],
                ['require_confirmation', 'Confirm'],
                ['enabled', 'Enabled'],
              ] as const).map(([key, label]) => (
                <ToggleRow
                  key={key}
                  label={label}
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
        ))}
      </div>

      <div className="mt-4 space-y-2 border-t border-white/[0.07] pt-4">
        <p className="section-label">Blocked roots always win</p>
        {draft.permissions.blocked_roots.map((root, index) => (
          <div key={`${root}-${index}`} className="flex gap-2">
            <input
              value={root}
              onChange={(e) => updateDraft((current) => {
                const blockedRoots = [...current.permissions.blocked_roots]
                blockedRoots[index] = e.target.value
                return { ...current, permissions: { ...current.permissions, blocked_roots: blockedRoots } }
              })}
              className="control min-w-0 flex-1 rounded-xl px-3 py-2 text-sm"
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
              className="ghost-button h-10 w-10 rounded-xl hover:text-red-300"
              aria-label={`Remove blocked root ${index + 1}`}
            >
              <Trash2 size={14} />
            </button>
          </div>
        ))}
        <div className="flex gap-2">
          <input
            value={blockedRootInput}
            onChange={(e) => setBlockedRootInput(e.target.value)}
            placeholder="Add blocked root"
            className="control min-w-0 flex-1 rounded-xl px-3 py-2 text-sm"
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
            className="primary-button h-10 w-10 rounded-xl"
            aria-label="Add blocked root"
          >
            <Plus size={14} />
          </button>
        </div>
      </div>
    </div>
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
    <div className="panel-muted rounded-xl p-3">
      <div className="mb-2.5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Bot size={13} className="text-neutral-400" />
          <p className="text-xs font-semibold text-neutral-200">App overrides</p>
        </div>
        <button
          type="button"
          onClick={() => updateDraft((current) => ({
            ...current,
            permissions: {
              ...current.permissions,
              app_rules: [...current.permissions.app_rules, emptyAppRule()],
            },
          }))}
          className="ghost-button rounded-lg px-2 py-1 text-xs"
        >
          <Plus size={12} />
          Add
        </button>
      </div>
      <div className="space-y-3">
        {draft.permissions.app_rules.map((rule, index) => (
          <div key={`app-rule-${index}`} className="rounded-xl border border-white/[0.07] bg-black/10 p-3">
            <div className="grid gap-2 sm:grid-cols-2">
              <input
                value={rule.alias}
                onChange={(e) => updateDraft((current) => {
                  const appRules = [...current.permissions.app_rules]
                  appRules[index] = { ...appRules[index], alias: e.target.value }
                  return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
                })}
                placeholder="Alias"
                className="control rounded-xl px-3 py-2 text-sm"
              />
              <input
                value={rule.display_name}
                onChange={(e) => updateDraft((current) => {
                  const appRules = [...current.permissions.app_rules]
                  appRules[index] = { ...appRules[index], display_name: e.target.value }
                  return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
                })}
                placeholder="Display name"
                className="control rounded-xl px-3 py-2 text-sm"
              />
            </div>
            <input
              value={rule.exe_paths.join(', ')}
              onChange={(e) => updateDraft((current) => {
                const appRules = [...current.permissions.app_rules]
                appRules[index] = {
                  ...appRules[index],
                  exe_paths: e.target.value.split(',').map((path) => path.trim()).filter(Boolean),
                }
                return { ...current, permissions: { ...current.permissions, app_rules: appRules } }
              })}
              placeholder="Exe paths (comma separated)"
              className="control mt-2 w-full rounded-xl px-3 py-2 text-sm"
            />
            <div className="mt-2 grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
              {([
                ['launch_allowed', 'Launch'],
                ['uia_allowed', 'UIA'],
                ['screen_fallback_allowed', 'Screen'],
                ['require_confirmation', 'Confirm'],
                ['enabled', 'Enabled'],
              ] as const).map(([key, label]) => (
                <ToggleRow
                  key={key}
                  label={label}
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
              className="ghost-button mt-3 rounded-lg px-2 py-1 text-xs hover:text-red-300"
            >
              <Trash2 size={12} />
              Remove app rule
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
