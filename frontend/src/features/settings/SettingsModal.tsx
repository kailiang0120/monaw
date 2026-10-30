import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertCircle,
  Bot,
  CheckCircle2,
  ChevronDown,
  Container,
  Copy,
  Download,
  Folder,
  Globe2,
  KeyRound,
  Loader2,
  Mic,
  Puzzle,
  RotateCcw,
  Save,
  Search,
  Shield,
  Trash2,
  UserRound,
  Wrench,
  X,
  type LucideIcon,
} from 'lucide-react'
import { ConnectionPortalPanel } from './connectionPortals/ConnectionPortalPanel'
import { McpSettings } from './McpSettings'
import { SandboxSettings } from './SandboxSettings'
import { useSettingsDraft } from './useSettingsDraft'
import { useDiscardPrompt } from '../../hooks/useDiscardPrompt'
import { useModalFocus } from '../../hooks/useModalFocus'
import {
  DEFAULT_CONNECTION_PORTAL,
  type ConnectionPortalId,
  type ConnectionSecretStatuses,
} from './connectionPortals/connectionPortalConfig'
import { Dropdown } from '../../components/Dropdown'
import {
  Badge,
  ChoiceCard,
  Note,
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
  FOOTER_COPY,
  IDENTITY_COPY,
  MODEL_COPY,
  PAGE_COPY,
  PERMISSION_MODE_COPY,
  SKILLS_COPY,
  type SettingsPageId,
} from './settingsCopy'
import {
  fetchBrowserDiagnostics,
  fetchMCPDiagnostics,
  installBrowserExtension,
  reconnectBrowser,
} from '../../lib/api/diagnostics'
import {
  deleteSpeechToTextModel,
  downloadSpeechToTextModel,
  fetchModelOptions,
  fetchOpenAIAccountStatus,
  fetchSettings,
  fetchSandboxStatus,
  fetchSpeechToTextStatus,
  fetchWorkspaceInstructions,
  offloadSpeechToTextModel,
  logoutOpenAIAccount,
  resetWorkspaceInstructions,
  startOpenAIAccountLogin,
  type OpenAIAccountStatus,
  updateWorkspaceInstructions,
} from '../../lib/api/settings'
import { syncStoredApiKeysToBackend } from '../../lib/apiKeySync'
import { DEFAULT_AGENT_NAME } from '../../lib/identity'
import type {
  AgentSettings,
  BrowserExtensionInstallResult,
  BrowserUseDiagnostics,
  MCPServerDiagnostics,
  SandboxStatus,
  SpeechToTextStatus,
} from '../../lib/api/types'
import {
  PERMISSION_PROFILES,
  applyPermissionProfile,
  duplicateMcpServerNames,
  FALLBACK_MODEL_OPTIONS,
  formatUnavailableReason,
  normalizeDraft,
  permissionProfileFromPermissions,
  type ModelOptionsCatalog,
} from './settingsConfig'

interface Props {
  onClose: () => void
  diagnosticsRefreshKey?: number
  observabilityRefreshKey?: number
}

type SettingsTab = SettingsPageId
type ToolsTab = 'skills' | 'mcp' | 'plugins'

const TOOLS_TABS: Array<{ id: ToolsTab; label: string }> = [
  { id: 'skills', label: 'Skills' },
  { id: 'mcp', label: 'MCP' },
  { id: 'plugins', label: 'Plugins' },
]

const TAB_ICONS: Record<SettingsTab, LucideIcon> = {
  identity: UserRound,
  apiKeys: KeyRound,
  model: Bot,
  tools: Wrench,
  browser: Globe2,
  permissions: Shield,
  sandbox: Container,
  observability: Activity,
}

/**
 * Group pages by what the user is trying to configure.
 */
const NAV_GROUPS: Array<{ id: string; label: string; items: SettingsTab[] }> = [
  { id: 'general', label: 'General', items: ['identity', 'apiKeys'] },
  { id: 'intelligence', label: 'Intelligence', items: ['model'] },
  { id: 'capabilities', label: 'Capabilities', items: ['tools', 'browser'] },
  { id: 'safety', label: 'Safety', items: ['permissions', 'sandbox'] },
  { id: 'inspect', label: 'Inspect', items: ['observability'] },
]

const ALL_TABS: SettingsTab[] = NAV_GROUPS.flatMap((group) => group.items)

const ObservabilityPanel = lazy(() =>
  import('./ObservabilityPanel').then((module) => ({ default: module.ObservabilityPanel })),
)

function accountUsageWindowLabel(minutes: number | null, window: 'primary' | 'secondary'): string {
  if (!minutes || minutes <= 0) return window === 'primary' ? 'Primary limit' : 'Secondary limit'
  if (minutes === 7 * 24 * 60) return 'Weekly limit'
  if (minutes % (24 * 60) === 0) return `${minutes / (24 * 60)}-day limit`
  if (minutes % 60 === 0) return `${minutes / 60}-hour limit`
  return `${minutes}-minute limit`
}

function accountResetTime(timestamp: number | null): string {
  if (!timestamp || !Number.isFinite(timestamp)) return 'Reset time unavailable'
  return `Resets ${new Date(timestamp * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`
}

export function SettingsModal({
  onClose,
  diagnosticsRefreshKey = 0,
  observabilityRefreshKey = 0,
}: Props) {
  const {
    draft,
    isDirty,
    secrets: connectionSecretValues,
    saving,
    saveError,
    load: loadDraft,
    updateDraft,
    setSecret: updateConnectionSecret,
    applyStoredCredentialStatus,
    reconcileMcpCredentials,
    save: saveDraft,
  } = useSettingsDraft()
  const [modelOptions, setModelOptions] = useState<ModelOptionsCatalog>(FALLBACK_MODEL_OPTIONS)
  const [activeTab, setActiveTab] = useState<SettingsTab>('model')
  const [toolsTab, setToolsTab] = useState<ToolsTab>('skills')
  const [navQuery, setNavQuery] = useState('')
  const [openaiAccount, setOpenaiAccount] = useState<OpenAIAccountStatus | null>(null)
  const [openaiAccountBusy, setOpenaiAccountBusy] = useState(false)
  const [refreshingOpenAIUsage, setRefreshingOpenAIUsage] = useState(false)
  const [openaiAccountError, setOpenaiAccountError] = useState('')
  const [openaiAccountAuthUrl, setOpenaiAccountAuthUrl] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [activeConnectionPortal, setActiveConnectionPortal] = useState<ConnectionPortalId>(DEFAULT_CONNECTION_PORTAL)
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
  const [reconnectingBrowser, setReconnectingBrowser] = useState(false)
  const [installingExtension, setInstallingExtension] = useState(false)
  const [extensionInstall, setExtensionInstall] = useState<BrowserExtensionInstallResult | null>(null)
  const [extensionError, setExtensionError] = useState('')
  const [extensionFolderCopied, setExtensionFolderCopied] = useState(false)
  const [extensionCopyError, setExtensionCopyError] = useState('')
  const [downloadingSpeechToText, setDownloadingSpeechToText] = useState(false)
  const [offloadingSpeechToText, setOffloadingSpeechToText] = useState(false)
  const [deletingSpeechToText, setDeletingSpeechToText] = useState(false)
  const [speechToTextError, setSpeechToTextError] = useState('')
  const [saved, setSaved] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null)
  const { confirming: confirmDiscard, ask: askDiscard, keepEditing, keepEditingRef } = useDiscardPrompt()

  const scrollRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    const refresh = async () => {
      try {
        const status = await fetchOpenAIAccountStatus(controller.signal)
        if (!controller.signal.aborted) {
          setOpenaiAccount(status)
          if (status.connected) setOpenaiAccountAuthUrl('')
        }
      } catch (error) {
        if (!controller.signal.aborted) setOpenaiAccountError(error instanceof Error ? error.message : 'Account status unavailable.')
      }
    }
    void refresh()
    if (openaiAccountAuthUrl) {
      const interval = window.setInterval(() => { void refresh() }, 2000)
      const timeout = window.setTimeout(() => {
        setOpenaiAccountAuthUrl('')
        setOpenaiAccountError('Sign-in timed out. Try again.')
      }, 10 * 60 * 1000)
      return () => { controller.abort(); window.clearInterval(interval); window.clearTimeout(timeout) }
    }
    return () => controller.abort()
  }, [openaiAccountAuthUrl])

  const signInOpenAIAccount = async () => {
    setOpenaiAccountBusy(true)
    setOpenaiAccountError('')
    try {
      const login = await startOpenAIAccountLogin()
      if (!/^https:\/\//i.test(login.auth_url)) throw new Error('Invalid sign-in URL returned.')
      setOpenaiAccountAuthUrl(login.auth_url)
      window.open(login.auth_url, '_blank', 'noopener,noreferrer')
    } catch (error) {
      setOpenaiAccountError(error instanceof Error ? error.message : 'Could not start sign-in.')
    } finally {
      setOpenaiAccountBusy(false)
    }
  }

  const signOutOpenAIAccount = async () => {
    setOpenaiAccountBusy(true)
    setOpenaiAccountError('')
    try {
      setOpenaiAccount(await logoutOpenAIAccount())
      setOpenaiAccountAuthUrl('')
    } catch (error) {
      setOpenaiAccountError(error instanceof Error ? error.message : 'Could not sign out.')
    } finally {
      setOpenaiAccountBusy(false)
    }
  }

  const refreshOpenAIUsage = async () => {
    setRefreshingOpenAIUsage(true)
    setOpenaiAccountError('')
    try {
      setOpenaiAccount(await fetchOpenAIAccountStatus())
    } catch (error) {
      setOpenaiAccountError(error instanceof Error ? error.message : 'Could not refresh usage limits.')
    } finally {
      setRefreshingOpenAIUsage(false)
    }
  }

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
      setBrowserDiagnostics(await fetchBrowserDiagnostics(signal))
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
        updateDraft((current) => normalizeDraft(current, loadedModelOptions))
      })

      try {
        const settings = normalizeDraft(await fetchSettings(signal))
        if (!cancelled) {
          setLoadError('')
          loadDraft(settings)
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
          applyStoredCredentialStatus(credentialStatus)
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

  const handleReconnectBrowser = async () => {
    setReconnectingBrowser(true)
    try {
      setBrowserDiagnostics(await reconnectBrowser())
    } catch {
      await loadBrowserDiagnostics()
    } finally {
      setReconnectingBrowser(false)
    }
  }

  const handleInstallExtension = async () => {
    setInstallingExtension(true)
    setExtensionError('')
    setExtensionFolderCopied(false)
    setExtensionCopyError('')
    try {
      const result = await installBrowserExtension()
      setExtensionInstall(result)
      setBrowserDiagnostics(result.status)
    } catch (error) {
      setExtensionError(error instanceof Error ? error.message : 'Could not set up the Monaw extension')
    } finally {
      setInstallingExtension(false)
    }
  }

  const copyExtensionFolder = async () => {
    if (!extensionInstall) return
    setExtensionCopyError('')
    setExtensionFolderCopied(false)
    try {
      if (window.electronAPI?.writeClipboardText) {
        await window.electronAPI.writeClipboardText(extensionInstall.folder)
      } else {
        await navigator.clipboard.writeText(extensionInstall.folder)
      }
      setExtensionFolderCopied(true)
    } catch {
      setExtensionFolderCopied(false)
      setExtensionCopyError('Could not copy automatically. Select the folder and press Ctrl+C (⌘C on Mac).')
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

  const chooseScreenshotsFolder = async () => {
    if (!draft) return
    const selected = await chooseDirectory(draft.browser.screenshots_dir)
    if (!selected) return
    updateDraft((current) => ({
      ...current,
      browser: { ...current.browser, screenshots_dir: selected },
    }))
  }

  const handleSave = async () => {
    if (!draft) return
    if (duplicateMcpServerNames(draft.mcp.servers).length > 0) return
    keepEditing()
    const outcome = await saveDraft(modelOptions)
    if (outcome === 'failed') return
    await Promise.all([loadMcpDiagnostics(), loadBrowserDiagnostics(), loadSandboxStatus()])
    if (outcome !== 'saved') return
    setSaved(true)
    setTimeout(() => setSaved(false), 2000)
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

  const refreshMcpCredentials = async () => {
    reconcileMcpCredentials(await fetchSettings())
    await loadMcpDiagnostics()
  }

  const requestClose = () => {
    if (isDirty) {
      askDiscard()
      return
    }
    onClose()
  }

  // Escape closes, but never silently throws away edits; at the discard prompt it keeps editing.
  useModalFocus(dialogRef, {
    onEscape: () => (confirmDiscard ? keepEditing() : requestClose()),
    remountKey: Boolean(draft),
  })

  // After setup, watch for the extension to connect once the user loads it in Chrome.
  const pollExtension = activeTab === 'browser'
    && Boolean(browserDiagnostics?.extension?.installed)
    && !browserDiagnostics?.extension?.connected
  useEffect(() => {
    if (!pollExtension) return
    const timer = window.setInterval(() => { void loadBrowserDiagnostics() }, 2000)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollExtension])

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
      <div className="st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-4">
        <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Settings" tabIndex={-1} className="settings-shell max-w-md rounded-2xl px-5 py-4">
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
  // Built-in skills are always on and hidden; only skills the user added are listed.
  const userSkills = draft.available_skills.filter((skill) => !skill.hidden && skill.tier !== 'internal')
  const browserSkillEnabled = !!draft.tools.skills['browser-use'] && (browserSkill?.available ?? true)
  const allowedDomainsInput = draft.browser.allowed_domains.join(', ')
  const extensionStatus = browserDiagnostics?.extension
  const waitingForExtension = Boolean(extensionStatus?.installed) && !extensionStatus?.connected
  const duplicateMcpNames = duplicateMcpServerNames(draft.mcp.servers)
  const connectionSecretStatuses: ConnectionSecretStatuses = {
    openai: draft.api_keys.has_openai_key,
    google: draft.api_keys.has_google_key,
    tavily: draft.api_keys.has_tavily_key,
    telegramBot: Boolean(draft.api_keys.has_telegram_bot_token),
  }
  const telegramAllowlistConfigured = Boolean(draft.telegram_allowed_user_ids.trim() || draft.telegram_allowed_chat_ids.trim())
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
      if (!browserDiagnostics.chrome_found || browserDiagnostics.last_error) return 'Degraded'
      return 'Ready'
    }
    return skill.recommended ? 'On by default' : 'Optional'
  }

  const skillHealthTone = (skill: AgentSettings['available_skills'][number]) => {
    const health = getSkillHealth(skill)
    if (health.startsWith('Unavailable') || health === 'Degraded') return 'warn' as const
    if (['Ready', 'On by default'].includes(health)) return 'ok' as const
    return 'neutral' as const
  }

  const footerMessage = duplicateMcpNames.length > 0
    ? { icon: AlertCircle, tone: 'danger' as const, text: FOOTER_COPY.duplicateMcp }
    : saveError
      ? { icon: AlertCircle, tone: 'danger' as const, text: saveError }
      : saved
        ? { icon: CheckCircle2, tone: 'ok' as const, text: FOOTER_COPY.saved }
        : isDirty
          ? { icon: AlertCircle, tone: 'warn' as const, text: FOOTER_COPY.dirty }
          : { icon: CheckCircle2, tone: 'neutral' as const, text: FOOTER_COPY.clean }

  return (
    <div
      className="st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-4"
      onClick={(e) => e.target === e.currentTarget && requestClose()}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label="Settings"
        tabIndex={-1}
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
                  <PageHeader title={PAGE_COPY.model.label} />
                  <div className="space-y-4">
                    <SettingsCard
                      title="Voice input"
                      action={
                        <Badge tone={speechToTextReady ? 'ok' : 'warn'}>
                          {speechToTextIsCloud
                            ? (speechToTextReady ? 'Cloud ready' : 'Key needed')
                            : (speechToTextReady ? 'Ready' : 'Download needed')}
                        </Badge>
                      }
                    >
                      <SettingRow label="Speech recognition" description={MODEL_COPY.speechEngine}>
                        <Dropdown<AgentSettings['speech_to_text']['engine']>
                          ariaLabel="Speech recognition"
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

                  </div>
                </>
              )}

              {activeTab === 'apiKeys' && (
                <>
                  <PageHeader title={PAGE_COPY.apiKeys.label} />
                  <div className="space-y-4">
                    <SettingsCard
                      title="OpenAI account"
                      description="Sign in with ChatGPT."
                      action={
                        <Badge tone={openaiAccount?.connected ? 'ok' : 'warn'}>
                          {openaiAccount?.connected ? `Connected${openaiAccount.plan ? ` · ${openaiAccount.plan}` : ''}` : 'Not connected'}
                        </Badge>
                      }
                    >
                      <div className="flex flex-wrap items-center gap-3 p-4">
                        {openaiAccount?.connected ? (
                          <>
                            <button type="button" className="st-btn st-btn-secondary" disabled={openaiAccountBusy} onClick={() => void signOutOpenAIAccount()}>
                              Sign out
                            </button>
                            <button type="button" className="st-btn st-btn-ghost" disabled={refreshingOpenAIUsage} onClick={() => void refreshOpenAIUsage()}>
                              <RotateCcw size={13} className={refreshingOpenAIUsage ? 'animate-spin' : ''} />
                              Refresh usage
                            </button>
                          </>
                        ) : (
                          <button type="button" className="st-btn st-btn-primary" disabled={openaiAccountBusy || !!openaiAccountAuthUrl} onClick={() => void signInOpenAIAccount()}>
                            {openaiAccountBusy ? 'Opening sign-in…' : openaiAccountAuthUrl ? 'Waiting for sign-in…' : 'Sign in with ChatGPT'}
                          </button>
                        )}
                        {openaiAccountAuthUrl && !openaiAccount?.connected && (
                          <a href={openaiAccountAuthUrl} target="_blank" rel="noopener noreferrer">Open sign-in page</a>
                        )}
                        {openaiAccountError && <p role="alert" className="text-sm text-red-600">{openaiAccountError}</p>}
                      </div>
                      {openaiAccount?.connected && (
                        <div className="px-4 pb-4">
                          {openaiAccount.limits?.length ? (
                            <div className="grid gap-2 sm:grid-cols-2">
                              {openaiAccount.limits.map((limit) => {
                                const remaining = limit.used_percent == null
                                  ? null
                                  : Math.max(0, Math.round(100 - limit.used_percent))
                                return (
                                  <div className="st-usage-window" key={`${limit.limit_id}-${limit.window}`}>
                                    <div className="flex items-center justify-between gap-3">
                                      <span className="st-label">
                                        {openaiAccount.limits.some((other) => other.limit_id !== limit.limit_id) ? `${limit.limit_name} · ` : ''}
                                        {accountUsageWindowLabel(limit.window_duration_mins, limit.window)}
                                      </span>
                                      <strong className="text-xs font-semibold" style={{ color: 'var(--st-accent-text)' }}>
                                        {remaining == null ? '—' : `${remaining}% left`}
                                      </strong>
                                    </div>
                                    {remaining != null && (
                                      <div className="st-usage-track" role="progressbar" aria-label={accountUsageWindowLabel(limit.window_duration_mins, limit.window)} aria-valuemin={0} aria-valuemax={100} aria-valuenow={remaining}>
                                        <div className="st-usage-fill" style={{ width: `${remaining}%` }} />
                                      </div>
                                    )}
                                    <p className="st-hint mt-2">{accountResetTime(limit.resets_at)}</p>
                                  </div>
                                )
                              })}
                            </div>
                          ) : (
                            <p className="st-hint">{openaiAccount.usage_error || 'Usage limits are not available for this account.'}</p>
                          )}
                        </div>
                      )}
                    </SettingsCard>
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
                        description="Leave both lists empty to block all messages."
                        action={
                          <Badge tone={telegramAllowlistConfigured ? 'ok' : 'warn'}>
                            {telegramAllowlistConfigured ? 'Configured' : 'Required'}
                          </Badge>
                        }
                      >
                        <StackedRow
                          label="Allowed user IDs"
                          description="Numeric IDs, separated by commas."
                        >
                          <input
                            value={draft.telegram_allowed_user_ids}
                            aria-label="Allowed user IDs"
                            onChange={(e) => {
                              const ids = e.target.value
                              updateDraft((current) => ({ ...current, telegram_allowed_user_ids: ids }))
                            }}
                            placeholder="123456789, 987654321"
                            className="st-input"
                          />
                        </StackedRow>
                        <StackedRow
                          label="Allowed chat IDs"
                          description="Group IDs usually start with -100."
                        >
                          <input
                            value={draft.telegram_allowed_chat_ids}
                            aria-label="Allowed chat IDs"
                            onChange={(e) => {
                              const ids = e.target.value
                              updateDraft((current) => ({ ...current, telegram_allowed_chat_ids: ids }))
                            }}
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
                  <PageHeader title={PAGE_COPY.identity.label} />
                  <div className="space-y-4">
                    <SettingsCard title="Names">
                      <SettingRow label="Agent name">
                        <input
                          value={draft.identity.agent_name}
                          aria-label="Agent name"
                          maxLength={80}
                          onChange={(e) => updateDraft((current) => ({
                            ...current,
                            identity: { ...current.identity, agent_name: e.target.value },
                          }))}
                          placeholder={DEFAULT_AGENT_NAME}
                          className="st-input"
                        />
                      </SettingRow>
                      <SettingRow label="Your name">
                        <input
                          value={draft.identity.user_name}
                          aria-label="Your name"
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

                    <SettingsCard title="Personalization">
                      <StackedRow label="About you">
                        <textarea
                          value={draft.identity.user_identity}
                          aria-label="About you"
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
                      <StackedRow label="Reply style">
                        <textarea
                          value={draft.identity.communication_style}
                          aria-label="Reply style"
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
                      <SwitchRow
                        label="Memory"
                        description={IDENTITY_COPY.memory}
                        checked={draft.memory.enabled}
                        onChange={(enabled) => updateDraft((current) => ({
                          ...current,
                          memory: { ...current.memory, enabled },
                        }))}
                      />
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

                  </div>
                </>
              )}

              {activeTab === 'tools' && (
                <>
                  <PageHeader
                    title={PAGE_COPY.tools.label}
                    description={PAGE_COPY.tools.blurb}
                  />
                  <div role="tablist" aria-label="Tool types" className="st-segment mb-5 w-full sm:w-auto">
                    {TOOLS_TABS.map((tab, index) => (
                      <button
                        key={tab.id}
                        type="button"
                        role="tab"
                        id={`settings-tools-tab-${tab.id}`}
                        aria-controls={`settings-tools-panel-${tab.id}`}
                        aria-selected={toolsTab === tab.id}
                        tabIndex={toolsTab === tab.id ? 0 : -1}
                        className="st-segment-item flex-1 sm:flex-none"
                        onClick={() => setToolsTab(tab.id)}
                        onKeyDown={(event) => {
                          const nextIndex = event.key === 'ArrowRight' ? (index + 1) % TOOLS_TABS.length
                            : event.key === 'ArrowLeft' ? (index + TOOLS_TABS.length - 1) % TOOLS_TABS.length
                              : event.key === 'Home' ? 0
                                : event.key === 'End' ? TOOLS_TABS.length - 1 : null
                          if (nextIndex === null) return
                          event.preventDefault()
                          const nextTab = TOOLS_TABS[nextIndex].id
                          setToolsTab(nextTab)
                          document.getElementById(`settings-tools-tab-${nextTab}`)?.focus()
                        }}
                      >
                        {tab.label}
                      </button>
                    ))}
                  </div>
                </>
              )}

              {activeTab === 'tools' && toolsTab === 'skills' && (
                <>
                  <div role="tabpanel" id="settings-tools-panel-skills" aria-labelledby="settings-tools-tab-skills" className="space-y-4" tabIndex={0}>
                    <Note tone="info">{SKILLS_COPY.intro}</Note>
                    <SettingsCard
                        title={SKILLS_COPY.title}
                        action={<Badge>{userSkills.length}</Badge>}
                      >
                        {userSkills.length === 0 ? (
                          <p className="st-hint p-4">{SKILLS_COPY.empty}</p>
                        ) : (
                          userSkills.map((skill) => {
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
                  </div>
                </>
              )}


              {activeTab === 'browser' && (
                <>
                  <PageHeader
                    title={PAGE_COPY.browser.label}
                    action={
                      <button
                        type="button"
                        onClick={handleReconnectBrowser}
                        disabled={reconnectingBrowser || !browserSkillEnabled}
                        title={BROWSER_COPY.reconnect}
                        className="st-btn st-btn-secondary"
                      >
                        <RotateCcw size={13} />
                        {reconnectingBrowser ? 'Reconnecting' : 'Reconnect'}
                      </button>
                    }
                  />
                  <div className="space-y-4">
                    <SettingsCard title="Status">
                      <div className="grid grid-cols-2">
                        <StatusCell
                          label="Chrome"
                          value={!browserDiagnostics ? 'Checking' : browserDiagnostics.chrome_found ? 'Found' : 'Not found'}
                        />
                        <StatusCell
                          label="Monaw extension"
                          value={extensionConnectionStatus(browserDiagnostics, browserSkillEnabled)}
                        />
                      </div>
                      {browserDiagnostics && !browserDiagnostics.chrome_found && (
                        <div className="st-row-stacked">
                          <Note tone="warn" icon={AlertCircle}>{BROWSER_COPY.chromeMissing}</Note>
                        </div>
                      )}
                      {browserDiagnostics?.last_error && (
                        <div className="st-row-stacked">
                          <Note tone="warn">{browserDiagnostics.last_error}</Note>
                        </div>
                      )}
                    </SettingsCard>

                    <SettingsCard
                        title="Monaw extension"
                        description={BROWSER_COPY.extension}
                        action={
                          <button
                            type="button"
                            onClick={handleInstallExtension}
                            disabled={installingExtension}
                            className={`st-btn ${extensionStatus?.installed ? 'st-btn-secondary' : 'st-btn-primary'}`}
                          >
                            {installingExtension ? <Loader2 size={13} className="animate-spin" /> : <Puzzle size={13} />}
                            {installingExtension
                              ? 'Setting up'
                              : extensionStatus?.update_available
                                ? 'Update extension'
                                : extensionStatus?.installed
                                  ? 'Set up again'
                                  : 'Set up extension'}
                          </button>
                        }
                      >
                        {extensionStatus?.update_available && (
                          <div className="st-row-stacked">
                            <Note tone="info">{BROWSER_COPY.extensionUpdate}</Note>
                          </div>
                        )}
                        {extensionError && (
                          <div className="st-row-stacked">
                            <Note tone="danger" icon={AlertCircle}>{extensionError}</Note>
                          </div>
                        )}
                        {extensionStatus?.last_error && !extensionStatus.connected && (
                          <div className="st-row-stacked">
                            <Note tone="warn">{extensionStatus.last_error}</Note>
                          </div>
                        )}
                        {extensionInstall && !extensionStatus?.connected && (
                          <div className="st-row-stacked space-y-3">
                            <ol className="list-decimal space-y-1 pl-5 text-sm text-[var(--text-primary)]">
                              {BROWSER_COPY.extensionSteps.map((step) => <li key={step}>{step}</li>)}
                            </ol>
                            <div className="flex items-center gap-2">
                              <input
                                readOnly
                                value={extensionInstall.folder}
                                aria-label="Extension folder"
                                className="st-input flex-1"
                                onFocus={(event) => event.currentTarget.select()}
                              />
                              <button type="button" onClick={copyExtensionFolder} className="st-btn st-btn-secondary">
                                <Copy size={13} />
                                {extensionFolderCopied ? 'Copied' : 'Copy'}
                              </button>
                            </div>
                            {extensionCopyError && <p role="alert" className="st-desc">{extensionCopyError}</p>}
                            {!extensionInstall.extensions_page_opened && (
                              <Note tone="info">Open chrome://extensions in Chrome yourself; Monaw could not open it.</Note>
                            )}
                            {waitingForExtension && (
                              <p className="st-desc flex items-center gap-2">
                                <Loader2 size={13} className="animate-spin" />
                                Waiting for the extension to connect…
                              </p>
                            )}
                          </div>
                        )}
                        {extensionStatus?.connected && (
                          <div className="st-row-stacked">
                            <Note tone="info" icon={CheckCircle2}>
                              Connected{extensionStatus.connected_version ? ` (version ${extensionStatus.connected_version})` : ''}. Monaw uses the Chrome profile the extension is loaded in.
                            </Note>
                          </div>
                        )}
                    </SettingsCard>

                    <SettingsCard title="My Chrome" description={BROWSER_COPY.usesYourProfile}>
                      <SettingRow label="Navigation target domains" description={BROWSER_COPY.allowedDomains} wide>
                        <input
                          value={allowedDomainsInput}
                          aria-label="Navigation target domains"
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

                    <SettingsCard title="Screenshots" description={BROWSER_COPY.screenshots}>
                      <StackedRow label="Screenshots folder">
                        <OutputFolderInput
                          label="Screenshots folder"
                          value={draft.browser.screenshots_dir}
                          onChange={(screenshots_dir) => updateDraft((current) => ({
                            ...current,
                            browser: { ...current.browser, screenshots_dir },
                          }))}
                          onBrowse={window.electronAPI?.selectDirectory ? chooseScreenshotsFolder : undefined}
                        />
                      </StackedRow>
                    </SettingsCard>
                  </div>
                </>
              )}

              {activeTab === 'tools' && toolsTab === 'mcp' && (
                <McpSettings
                  mcp={draft.mcp}
                  diagnostics={mcpDiagnostics}
                  onChange={(updater) => updateDraft((current) => ({ ...current, mcp: updater(current.mcp) }))}
                  onDiagnostic={(name, diagnostic) => setMcpDiagnostics((current) => ({ ...current, [name]: diagnostic }))}
                  credentialsDisabled={isDirty || saving}
                  onCredentialsChanged={refreshMcpCredentials}
                />
              )}

              {activeTab === 'tools' && toolsTab === 'plugins' && (
                <div role="tabpanel" id="settings-tools-panel-plugins" aria-labelledby="settings-tools-tab-plugins" tabIndex={0}>
                  <SettingsCard title="Plugins">
                    <div className="flex items-start gap-3 p-5">
                      <Puzzle size={20} className="mt-0.5 shrink-0" style={{ color: 'var(--st-text-faint)' }} />
                      <div className="min-w-0">
                        <p className="st-label">Plugin installation is not available yet</p>
                        <p className="st-desc mt-1">Monaw currently supports built-in skills and external MCP connections.</p>
                        <button type="button" className="st-btn st-btn-secondary mt-3" onClick={() => setToolsTab('mcp')}>
                          Manage MCP connections
                        </button>
                      </div>
                    </div>
                  </SettingsCard>
                </div>
              )}

              {activeTab === 'observability' && (
                <>
                  <PageHeader title={PAGE_COPY.observability.label} />
                  <Suspense fallback={null}>
                    <ObservabilityPanel refreshKey={observabilityRefreshKey} />
                  </Suspense>
                </>
              )}

              {activeTab === 'permissions' && (
                <>
                  <PageHeader title={PAGE_COPY.permissions.label} />
                  <div className="space-y-4">
                    <SettingsCard title="Approval profile">
                      <div className="grid gap-2 p-4 sm:grid-cols-2">
                        {(['default', 'full_access', 'auto_review', 'custom'] as const).map((mode) => (
                          <ChoiceCard
                            key={mode}
                            ariaLabel={PERMISSION_MODE_COPY[mode].title}
                            title={PERMISSION_MODE_COPY[mode].title}
                            summary={PERMISSION_MODE_COPY[mode].summary}
                            description={PERMISSION_MODE_COPY[mode].description}
                            hint={PERMISSION_MODE_COPY[mode].recommendation}
                            tone={mode === 'default' ? 'ok' : mode === 'full_access' ? 'danger' : 'neutral'}
                            selected={draft.permissions.mode === mode}
                            onSelect={() => updatePermissionMode(mode)}
                          />
                        ))}
                      </div>
                    </SettingsCard>
                    <SettingsCard title={`${PERMISSION_MODE_COPY[draft.permissions.mode].title} allows`}>
                      <ul className="space-y-2 p-4 text-sm text-[var(--text-primary)]">
                        {PERMISSION_MODE_COPY[draft.permissions.mode].rules.map((rule) => (
                          <li key={rule} className="flex gap-2"><CheckCircle2 size={14} className="mt-0.5 shrink-0" />{rule}</li>
                        ))}
                      </ul>
                      <div className="px-4 pb-4 st-desc">Folder and app restrictions apply in every mode. Sandbox isolation is configured separately.</div>
                    </SettingsCard>
                    {draft.permissions.mode === 'custom' && (
                      <SettingsCard title="Custom config">
                        <div className="space-y-3 p-4">
                          <p className="st-desc">Edit <code>permissions.custom_profile</code> in this file. Save it, then reload settings to see your changes.</p>
                          <ReadOnlyValue label="Config file" value={draft.permissions_config_path || 'Runtime settings.json'} />
                          <details>
                            <summary className="st-desc cursor-pointer">View current config</summary>
                            <pre className="mt-3 overflow-x-auto rounded-lg bg-[var(--bg-secondary)] p-3 text-xs">{JSON.stringify({ permissions: { mode: 'custom', custom_profile: draft.permissions.custom_profile } }, null, 2)}</pre>
                          </details>
                        </div>
                      </SettingsCard>
                    )}
                  </div>
                </>
              )}

              {activeTab === 'sandbox' && (
                <SandboxSettings
                  sandbox={draft.sandbox}
                  status={sandboxStatus}
                  onChange={(updater) => updateDraft((current) => ({ ...current, sandbox: updater(current.sandbox) }))}
                  onStatus={setSandboxStatus}
                />
              )}
            </div>
          </div>

          {/* --------------------------------------------------------- footer */}
          <footer className="st-divider-t flex items-center justify-between gap-3 px-5 py-3.5">
            {confirmDiscard ? (
              <>
                <p className="st-desc">Close settings and lose your unsaved changes?</p>
                <div className="flex items-center gap-2">
                  <button ref={keepEditingRef} type="button" onClick={keepEditing} className="st-btn st-btn-ghost">
                    Keep editing
                  </button>
                  <button type="button" onClick={onClose} className="st-btn st-btn-danger">
                    Discard changes
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="st-desc flex items-center gap-1.5" role={saveError ? 'alert' : undefined}>
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
function extensionConnectionStatus(diagnostics: BrowserUseDiagnostics | null, skillEnabled: boolean): string {
  if (!skillEnabled) return 'Off'
  if (!diagnostics) return 'Checking'
  const extension = diagnostics.extension
  if (!extension?.installed) return 'Not set up'
  if (extension.connected) return 'Connected'
  // The built-in browser server the extension connects to.
  if (diagnostics.devtools.state === 'failed' || diagnostics.devtools.state === 'unhealthy') return 'Needs attention'
  return 'Waiting for Chrome'
}
