import { useState, useEffect, useCallback } from 'react'
import { Sidebar } from './components/Sidebar'
import { ChatWindow } from './components/ChatWindow'
import { InputBar } from './components/InputBar'
import { SettingsModal } from './features/settings/SettingsModal'
import { ApprovalToast } from './components/ApprovalToast'
import { AccessGrantDialog } from './components/AccessGrantDialog'
import { ScheduledTaskDialog } from './components/scheduling/ScheduledTaskDialog'
import { useChat } from './hooks/useChat'
import { deleteConversation, fetchConversations, renameConversation } from './lib/api/conversations'
import {
  deleteScheduledTask,
  fetchScheduledTasks,
  runScheduledTaskNow,
  updateScheduledTask,
} from './lib/api/scheduledTasks'
import { fetchSettings, updateSettings } from './lib/api/settings'
import { syncStoredApiKeysToBackendWithRetry } from './lib/apiKeySync'
import { DEFAULT_AGENT_NAME, resolveAgentName } from './lib/identity'
import type { AgentSettings, Conversation, ScheduledTask, UploadedAttachment } from './lib/api/types'

type ApprovalMode = AgentSettings['permissions']['mode']
type ThemeMode = 'dark' | 'light'

const THEME_STORAGE_KEY = 'agent_theme'

function initialTheme(): ThemeMode {
  if (typeof window === 'undefined') return 'dark'
  return window.localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

export default function App() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeConvId, setActiveConvId] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false)
  const [theme, setTheme] = useState<ThemeMode>(() => initialTheme())
  const [approvalMode, setApprovalMode] = useState<ApprovalMode>('default')
  const [agentName, setAgentName] = useState(DEFAULT_AGENT_NAME)
  const [savingApprovalMode, setSavingApprovalMode] = useState(false)
  const [scheduledTasks, setScheduledTasks] = useState<ScheduledTask[]>([])
  const [showScheduledTaskDialog, setShowScheduledTaskDialog] = useState(false)
  const [editingScheduledTaskId, setEditingScheduledTaskId] = useState<string | null>(null)

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

  const loadConversations = useCallback(async () => {
    try {
      const convs = await fetchConversations()
      setConversations(convs.sort((a, b) => b.created_at.localeCompare(a.created_at)))
    } catch {
      // Backend may not be ready yet
    }
  }, [])

  const loadScheduledTasks = useCallback(async () => {
    try {
      const tasks = await fetchScheduledTasks()
      setScheduledTasks(tasks)
    } catch {
      // Backend may not be ready yet.
    }
  }, [])

  useEffect(() => {
    syncStoredApiKeysToBackendWithRetry()
    loadConversations()
    loadScheduledTasks()
    const interval = setInterval(loadConversations, 5000)
    const scheduledInterval = setInterval(loadScheduledTasks, 15000)
    return () => {
      clearInterval(interval)
      clearInterval(scheduledInterval)
    }
  }, [loadConversations, loadScheduledTasks])

  useEffect(() => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme)
    document.documentElement.style.colorScheme = theme
    void window.electronAPI?.setTheme?.(theme)
  }, [theme])

  useEffect(() => {
    let cancelled = false

    const loadSettings = async () => {
      try {
        const settings = await fetchSettings()
        if (!cancelled) {
          setApprovalMode(settings.permissions.mode)
          setAgentName(resolveAgentName(settings.identity?.agent_name))
        }
      } catch {
        if (!cancelled) {
          setApprovalMode('default')
          setAgentName(DEFAULT_AGENT_NAME)
        }
      }
    }

    loadSettings()

    return () => {
      cancelled = true
    }
  }, [])

  const refreshVisibleSettings = async () => {
    try {
      const settings = await fetchSettings()
      setApprovalMode(settings.permissions.mode)
      setAgentName(resolveAgentName(settings.identity?.agent_name))
    } catch {
      // Keep the last visible settings if refresh fails.
    }
  }

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
    <div className={`app-surface ${theme === 'light' ? 'theme-light' : 'theme-dark'} flex h-screen w-screen overflow-hidden`}>
      <Sidebar
        conversations={conversations}
        activeId={activeConvId}
        collapsed={isSidebarCollapsed}
        onToggleCollapsed={() => setIsSidebarCollapsed((value) => !value)}
        onSelect={handleSelectConversation}
        onNew={handleNewChat}
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
        agentName={agentName}
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
            conversationTitle={activeConversation?.title}
            isStreaming={isStreaming}
            agentName={agentName}
            onLoadOlderMessages={loadOlderMessages}
            onRename={activeConvId ? (title) => handleRenameConversation(activeConvId, title) : undefined}
            onPromptSelect={handleSend}
          />
          <InputBar
            onSend={handleSend}
            onStop={stopStreaming}
            isStreaming={isStreaming}
            conversationId={activeConvId}
            contextRefreshKey={messages.length}
            disabled={isLoadingHistory}
            approvalMode={approvalMode}
            approvalModeDisabled={savingApprovalMode}
            onApprovalModeChange={handleApprovalModeChange}
          />
        </main>
      </div>

      <ApprovalToast conversationId={activeConvId} isStreaming={isStreaming} />

      {pendingAccessGrant && (
        <AccessGrantDialog
          ticket={pendingAccessGrant}
          onResolved={() => setPendingAccessGrant(null)}
        />
      )}

      {showSettings && (
        <SettingsModal
          onClose={() => {
            setShowSettings(false)
            void refreshVisibleSettings()
          }}
        />
      )}

      {showScheduledTaskDialog && (
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
      )}
    </div>
  )
}
