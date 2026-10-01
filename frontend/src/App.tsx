import { Suspense, lazy, useState, useEffect, useCallback, useRef } from 'react'
import { AppBar } from './components/AppBar'
import { Sidebar } from './components/Sidebar'
import { ChatWindow } from './components/ChatWindow'
import { InputBar } from './components/InputBar'
import { ApprovalToast } from './components/ApprovalToast'
import { AccessGrantCard } from './components/AccessGrantCard'
import { useChat } from './hooks/useChat'
import { deleteConversation, fetchConversations, renameConversation } from './lib/api/conversations'
import {
  deleteScheduledTask,
  fetchScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
} from './lib/api/scheduledTasks'
import { fetchModelOptions, fetchSettings, updateSettings } from './lib/api/settings'
import { FALLBACK_MODEL_OPTIONS, reasoningEffortsForProvider } from './features/settings/settingsConfig'
import { syncStoredApiKeysToBackendWithRetry } from './lib/apiKeySync'
import { fetchPendingAccessGrants } from './lib/api/accessGrants'
import { subscribeServerEvents, type ServerEvent } from './lib/api/serverEvents'
import { DEFAULT_AGENT_NAME, resolveAgentName } from './lib/identity'
import type { AgentSettings, Conversation, ModelOptions, ScheduledTask, UploadedAttachment } from './lib/api/types'

type ApprovalMode = AgentSettings['permissions']['mode']
type ThemeMode = 'dark' | 'light'
type ModelSelection = Pick<AgentSettings['llm'], 'provider' | 'model_name' | 'reasoning_effort'>

const THEME_STORAGE_KEY = 'agent_theme'
const STARTUP_REFRESH_RETRIES = 20
const STARTUP_REFRESH_DELAY_MS = 1000
const RECOVERY_POLL_MS = 60_000

const SettingsModal = lazy(() =>
  import('./features/settings/SettingsModal').then((module) => ({ default: module.SettingsModal })),
)
const ScheduledTaskDialog = lazy(() =>
  import('./components/scheduling/ScheduledTaskDialog').then((module) => ({
    default: module.ScheduledTaskDialog,
  })),
)

function initialTheme(): ThemeMode {
  if (typeof window === 'undefined') return 'dark'
  return window.localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

function sameConversations(a: Conversation[], b: Conversation[]): boolean {
  return a.length === b.length && a.every((item, index) => {
    const other = b[index]
    return other !== undefined
      && item.id === other.id
      && item.title === other.title
      && item.created_at === other.created_at
  })
}

function sameScheduledTasks(a: ScheduledTask[], b: ScheduledTask[]): boolean {
  if (a.length !== b.length) return false
  return a.every((item, index) => {
    const other = b[index]
    return other !== undefined
      && item.id === other.id
      && item.updatedAt === other.updatedAt
      && item.nextRunAt === other.nextRunAt
      && item.lastRunAt === other.lastRunAt
      && item.lastRunStatus === other.lastRunStatus
      && item.running === other.running
  })
}

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeConvId, setActiveConvId] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false)
  const [isBackendReady, setIsBackendReady] = useState(() => typeof window === 'undefined' || !window.electronAPI?.isElectron)
  const [theme, setTheme] = useState<ThemeMode>(() => initialTheme())
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>('default')
  const [modelSelection, setModelSelection] = useState<ModelSelection>({
    provider: 'openai', model_name: 'gpt-6-luna', reasoning_effort: 'medium',
  })
  const [modelOptions, setModelOptions] = useState<ModelOptions>(FALLBACK_MODEL_OPTIONS)
  const [hasGoogleKey, setHasGoogleKey] = useState(false)
  const [savingModelSelection, setSavingModelSelection] = useState(false)
  const [modelSelectionError, setModelSelectionError] = useState('')
  const [composerFocusRequestKey, setComposerFocusRequestKey] = useState(0)
  const [agentName, setAgentName] = useState(DEFAULT_AGENT_NAME)
  const [savingApprovalMode, setSavingApprovalMode] = useState(false)
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTask[]>([])
  const [showScheduledTaskDialog, setShowScheduledTaskDialog] = useState(false)
  const [editingScheduledTaskId, setEditingScheduledTaskId] = useState<string | null>(null)
  const [eventChannelConnected, setEventChannelConnected] = useState(false)
  const [approvalRefreshKey, setApprovalRefreshKey] = useState(0)
  const [usageRefreshKey, setUsageRefreshKey] = useState(0)
  const [memoryRefreshKey, setMemoryRefreshKey] = useState(0)
  const [observabilityRefreshKey, setObservabilityRefreshKey] = useState(0)
  const [diagnosticsRefreshKey, setDiagnosticsRefreshKey] = useState(0)
  const activeConvIdRef = useRef<string | null>(activeConvId)

  const {
    messages,
    isStreaming,
    isLoadingHistory,
    isLoadingOlderHistory,
    hasMoreHistory,
    pendingAccessGrant,
    setPendingAccessGrant,
    loadOlderMessages,
    sendMessage,
    refreshMessages,
    stopStreaming,
    clearMessages,
  } = useChat(activeConvId)

  useEffect(() => {
    activeConvIdRef.current = activeConvId
  }, [activeConvId])

  const loadConversations = useCallback(async () => {
    try {
      const convs = await fetchConversations()
      const sorted = [...convs].sort((a, b) => b.created_at.localeCompare(a.created_at))
      setIsBackendReady(true)
      setConversations((current) => (sameConversations(current, sorted) ? current : sorted))
      return true
    } catch {
      // Backend may not be ready yet
      return false
    }
  }, [])

  const loadScheduledTasks = useCallback(async () => {
    try {
      const tasks = await fetchScheduledTasks()
      setIsBackendReady(true)
      setScheduledTasks((current) => (sameScheduledTasks(current, tasks) ? current : tasks))
      return true
    } catch {
      // Backend may not be ready yet.
      return false
    }
  }, [])

  const loadVisibleSettings = useCallback(async () => {
    try {
      const settings = await fetchSettings()
      setIsBackendReady(true)
      setApprovalMode(settings.permissions.mode)
      setModelSelection({
        provider: settings.llm.provider,
        model_name: settings.llm.model_name,
        reasoning_effort: settings.llm.reasoning_effort,
      })
      setHasGoogleKey(settings.api_keys.has_google_key)
      setAgentName(resolveAgentName(settings.identity?.agent_name))
      return true
    } catch {
      return false
    }
  }, [])

  const refreshVisibleSettings = async () => {
    await loadVisibleSettings()
  }

  useEffect(() => {
    const controller = new AbortController()
    void fetchModelOptions(controller.signal).then(setModelOptions).catch(() => {})
    return () => controller.abort()
  }, [])

  const loadPendingAccessGrant = useCallback(async () => {
    try {
      const pending = await fetchPendingAccessGrants(activeConvIdRef.current ?? undefined, 1)
      const ticket = pending[0]
      setPendingAccessGrant(ticket ? {
        ticket_id: ticket.id,
        target_type: ticket.target_type,
        target_identifier: ticket.target_identifier,
        display_name: ticket.display_name,
        action_context: ticket.action_context,
        requested_access: ticket.requested_access,
      } : null)
    } catch {
      // Event-driven refresh should never interrupt the chat UI.
    }
  }, [setPendingAccessGrant])

  useEffect(() => {
    let cancelled = false
    let retryTimer: number | null = null
    let attempts = 0

    void syncStoredApiKeysToBackendWithRetry().then(() => {
      if (!cancelled) void loadVisibleSettings()
    })

    const refreshStartupData = async () => {
      const [conversationsOk, scheduledTasksOk, settingsOk] = await Promise.all([
        loadConversations(),
        loadScheduledTasks(),
        loadVisibleSettings(),
      ])
      if (cancelled || (conversationsOk && scheduledTasksOk && settingsOk)) return
      if (attempts >= STARTUP_REFRESH_RETRIES) return
      attempts += 1
      retryTimer = window.setTimeout(refreshStartupData, STARTUP_REFRESH_DELAY_MS)
    }

    void refreshStartupData()
    return () => {
      cancelled = true
      if (retryTimer) window.clearTimeout(retryTimer)
    }
  }, [loadConversations, loadScheduledTasks, loadVisibleSettings])

  useEffect(() => {
    const handleServerEvent = (event: ServerEvent) => {
      const eventConversationId = typeof event.data.conversation_id === 'string'
        ? event.data.conversation_id
        : ''
      const affectsActiveConversation = !eventConversationId || eventConversationId === activeConvIdRef.current

      if (event.event === 'backend.ready') {
        setIsBackendReady(true)
        void loadConversations()
        void loadScheduledTasks()
        void loadVisibleSettings()
        return
      }
      if (event.event === 'conversation.changed') {
        void loadConversations()
        if (affectsActiveConversation) {
          refreshMessages()
          setUsageRefreshKey((value) => value + 1)
        }
        return
      }
      if (event.event === 'scheduled_tasks.changed') {
        void loadScheduledTasks()
        return
      }
      if (event.event === 'approval_ticket.created' || event.event === 'approval_ticket.changed') {
        setApprovalRefreshKey((value) => value + 1)
        return
      }
      if (event.event === 'access_grant.created' || event.event === 'access_grant.changed') {
        // Reload rather than clear so a second waiting grant takes the card's place.
        if (affectsActiveConversation) void loadPendingAccessGrant()
        return
      }
      if (event.event === 'usage.changed') {
        if (affectsActiveConversation) setUsageRefreshKey((value) => value + 1)
        return
      }
      if (event.event === 'memory.changed') {
        setMemoryRefreshKey((value) => value + 1)
        return
      }
      if (event.event === 'observability.changed') {
        setObservabilityRefreshKey((value) => value + 1)
        return
      }
      if (event.event === 'settings.changed') {
        void loadVisibleSettings()
        setDiagnosticsRefreshKey((value) => value + 1)
        return
      }
      if (event.event === 'mcp.changed') {
        setDiagnosticsRefreshKey((value) => value + 1)
      }
    }

    return subscribeServerEvents({
      onEvent: handleServerEvent,
      onStatus: setEventChannelConnected,
    })
  }, [
    loadConversations,
    loadPendingAccessGrant,
    loadScheduledTasks,
    loadVisibleSettings,
    refreshMessages,
  ])

  useEffect(() => {
    if (eventChannelConnected) return
    const refreshWhenVisible = () => {
      if (document.visibilityState !== 'visible') return
      void loadConversations()
      void loadScheduledTasks()
      void loadVisibleSettings()
      setApprovalRefreshKey((value) => value + 1)
      setUsageRefreshKey((value) => value + 1)
    }
    refreshWhenVisible()
    const interval = window.setInterval(refreshWhenVisible, RECOVERY_POLL_MS)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [eventChannelConnected, loadConversations, loadScheduledTasks, loadVisibleSettings])

  useEffect(() => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
    document.documentElement.style.colorScheme = theme
    void window.electronAPI?.setTheme?.(theme)
  }, [theme])

  const handleApprovalModeChange = async (mode: ApprovalMode) => {
    if (mode === approvalMode || savingApprovalMode) return
    const previous = approvalMode
    setApprovalMode(mode)
    setSavingApprovalMode(true)
    try {
      const settings = await updateSettings({ controller_permission_mode: mode })
      setApprovalMode(settings.permissions.mode)
    } catch {
      setApprovalMode(previous)
    } finally {
      setSavingApprovalMode(false)
    }
  }

  const handleModelSelectionChange = async (next: ModelSelection) => {
    if (savingModelSelection) return
    const previous = modelSelection
    const supportedEfforts = reasoningEffortsForProvider(next.provider, next.model_name)
    const normalized = {
      ...next,
      reasoning_effort: supportedEfforts.includes(next.reasoning_effort)
        ? next.reasoning_effort
        : supportedEfforts.includes('medium') ? 'medium' as const : supportedEfforts[0],
    }
    setModelSelection(normalized)
    setModelSelectionError('')
    setSavingModelSelection(true)
    try {
      const settings = await updateSettings({
        model_provider: normalized.provider,
        model_name: normalized.model_name,
        reasoning_effort: normalized.reasoning_effort,
      })
      setModelSelection({
        provider: settings.llm.provider,
        model_name: settings.llm.model_name,
        reasoning_effort: settings.llm.reasoning_effort,
      })
    } catch (error) {
      setModelSelection(previous)
      setModelSelectionError(error instanceof Error ? error.message : 'Could not change model')
    } finally {
      setSavingModelSelection(false)
    }
  }

  const handleSend = (text: string, attachments: UploadedAttachment[] = []) => {
    sendMessage(text, attachments, (newConvId) => {
      setActiveConvId(newConvId)
      loadConversations()
    })
  }

  const handleSelectConversation = (id: string) => {
    if (id === activeConvId) return
    setActiveConvId(id)
  }

  const handleRefreshChat = () => {
    refreshMessages()
    loadConversations()
  }

  const handleNewChat = () => {
    setActiveConvId(null)
    clearMessages()
  }

  const handleDeleteConversation = async (id: string) => {
    await deleteConversation(id)
    if (id === activeConvId) {
      setActiveConvId(null)
      clearMessages()
    }
    loadConversations()
  }

  const handleRenameConversation = async (id: string, title: string) => {
    try {
      const updated = await renameConversation(id, title)
      setConversations((prev) =>
        prev.map((c) => (c.id === id ? { ...c, title: updated.title } : c)),
      )
    } catch {
      // Silent — the sidebar will revert to the original title on next refresh
    }
  }

  const openScheduledTaskCreate = () => {
    setEditingScheduledTaskId(null)
    setShowScheduledTaskDialog(true)
  }

  const openScheduledTaskEdit = (id: string) => {
    setEditingScheduledTaskId(id)
    setShowScheduledTaskDialog(true)
  }

  const handleSelectScheduledRun = (conversationId: string) => {
    setActiveConvId(conversationId)
    loadConversations()
  }

  const handleToggleScheduledTaskEnabled = async (id: string, enabled: boolean) => {
    const previous = scheduledTasks
    setScheduledTasks((tasks) => tasks.map((task) => (task.id === id ? { ...task, enabled } : task)))
    try {
      const updated = await updateScheduledTask(id, { enabled })
      setScheduledTasks((tasks) => tasks.map((task) => (task.id === id ? updated : task)))
    } catch {
      setScheduledTasks(previous)
    }
  }

  const handleDeleteScheduledTask = async (id: string) => {
    const previous = scheduledTasks
    setScheduledTasks((tasks) => tasks.filter((task) => task.id !== id))
    try {
      await deleteScheduledTask(id)
    } catch {
      setScheduledTasks(previous)
    }
  }

  const handleRunScheduledTask = async (id: string) => {
    try {
      const result = await runScheduledTaskNow(id)
      setActiveConvId(result.conversationId)
      loadScheduledTasks()
      loadConversations()
    } catch {
      // The task row will refresh on the next polling cycle.
    }
  }

  const activeConversation = conversations.find((conversation) => conversation.id === activeConvId)
  const editingScheduledTask = editingScheduledTaskId
    ? scheduledTasks.find((task) => task.id === editingScheduledTaskId) || null
    : null

  return (
    <div className={`app-surface ${theme === 'light' ? 'theme-light' : 'theme-dark'} flex h-screen w-screen flex-col overflow-hidden`}>
      <AppBar
        sidebarCollapsed={isSidebarCollapsed}
        onToggleSidebar={() => setIsSidebarCollapsed((value) => !value)}
        onNewChat={handleNewChat}
        agentName={agentName}
        conversationTitle={activeConversation?.title}
        onRenameConversation={activeConvId ? (title) => handleRenameConversation(activeConvId, title) : undefined}
        status={!isBackendReady ? 'starting' : isStreaming ? 'working' : 'ready'}
        approvalMode={approvalMode}
        onOpenPermissions={() => setShowSettings(true)}
      />
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <Sidebar
          conversations={conversations}
          activeId={activeConvId}
          collapsed={isSidebarCollapsed}
          onToggleCollapsed={() => setIsSidebarCollapsed((value) => !value)}
          onSelect={handleSelectConversation}
          onDelete={handleDeleteConversation}
          onRename={(id, title) => handleRenameConversation(id, title)}
          onRefreshConversation={(id) => {
            if (id === activeConvId) handleRefreshChat()
          }}
          onOpenSettings={() => setShowSettings(true)}
          scheduledTasks={scheduledTasks}
          onOpenScheduledTaskCreate={openScheduledTaskCreate}
          onOpenScheduledTaskEdit={openScheduledTaskEdit}
          onSelectScheduledRun={handleSelectScheduledRun}
          onToggleScheduledTaskEnabled={handleToggleScheduledTaskEnabled}
          onDeleteScheduledTask={handleDeleteScheduledTask}
          onRunScheduledTask={handleRunScheduledTask}
          theme={theme}
          onToggleTheme={() => setTheme((value) => (value === 'light' ? 'dark' : 'light'))}
        />

        <div className="flex min-w-0 flex-1 flex-col">
          <main className="flex min-h-0 min-w-0 flex-1 flex-col">
            <ChatWindow
              messages={messages}
              isLoadingHistory={isLoadingHistory}
              isLoadingOlderHistory={isLoadingOlderHistory}
              hasMoreHistory={hasMoreHistory}
              conversationId={activeConvId}
              isStreaming={isStreaming}
              agentName={agentName}
              onLoadOlderMessages={loadOlderMessages}
              onPromptSelect={handleSend}
            />
            {pendingAccessGrant && (
              <AccessGrantCard
                key={pendingAccessGrant.ticket_id}
                ticket={pendingAccessGrant}
                onResolved={() => {
                  setPendingAccessGrant(null)
                  void loadPendingAccessGrant()
                }}
              />
            )}
            <InputBar
              onSend={handleSend}
              onStop={stopStreaming}
              isStreaming={isStreaming}
              conversationId={activeConvId}
              contextRefreshKey={messages.length}
              usageRefreshKey={usageRefreshKey}
              disabled={!isBackendReady}
              disabledReason={!isBackendReady ? 'Waiting for the local backend to finish starting…' : undefined}
              approvalMode={approvalMode}
              modelSelection={modelSelection}
              modelOptions={modelOptions}
              hasGoogleKey={hasGoogleKey}
              modelSelectionDisabled={savingModelSelection}
              modelSelectionError={modelSelectionError}
              onModelSelectionChange={handleModelSelectionChange}
              focusRequestKey={composerFocusRequestKey}
              approvalModeDisabled={savingApprovalMode}
              onApprovalModeChange={handleApprovalModeChange}
            />
          </main>
        </div>
      </div>

      <ApprovalToast
        conversationId={activeConvId}
        isStreaming={isStreaming}
        refreshKey={approvalRefreshKey}
      />

      {showSettings && (
        <Suspense fallback={null}>
          <SettingsModal
            diagnosticsRefreshKey={diagnosticsRefreshKey}
            memoryRefreshKey={memoryRefreshKey}
            observabilityRefreshKey={observabilityRefreshKey}
            onClose={() => {
              setShowSettings(false)
              setComposerFocusRequestKey((value) => value + 1)
              void refreshVisibleSettings()
            }}
          />
        </Suspense>
      )}

      {showScheduledTaskDialog && (
        <Suspense fallback={null}>
          <ScheduledTaskDialog
            task={editingScheduledTask}
            onClose={() => {
              setShowScheduledTaskDialog(false)
              setEditingScheduledTaskId(null)
            }}
            onSaved={() => {
              void loadScheduledTasks()
              void loadConversations()
            }}
            onRunConversation={(conversationId) => {
              setActiveConvId(conversationId)
              void loadConversations()
            }}
          />
        </Suspense>
      )}
    </div>
  )
}
