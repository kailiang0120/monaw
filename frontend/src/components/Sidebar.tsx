import { useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, Clock3, MessageSquare, Moon, Plus, RefreshCw, Settings, Sun, Trash2 } from 'lucide-react'
import type { Conversation, ScheduledTask } from '../lib/api/types'
import { DEFAULT_AGENT_NAME, resolveAgentName } from '../lib/identity'
import { ScheduledTasksList } from './scheduling/ScheduledTasksList'
import appLogo from '../assets/Logo.png'

type ThemeMode = 'dark' | 'light'

interface Props {
  conversations: Conversation[]
  activeId: string | null
  collapsed: boolean
  onToggleCollapsed: () => void
  onSelect: (id: string) => void
  onNew: () => void
  onDelete: (id: string) => void
  onRename?: (id: string, title: string) => void
  onRefreshConversation?: (id: string) => void
  onOpenSettings: () => void
  scheduledTasks: ScheduledTask[]
  onOpenScheduledTaskCreate: () => void
  onOpenScheduledTaskEdit: (id: string) => void
  onSelectScheduledRun: (conversationId: string) => void
  onToggleScheduledTaskEnabled: (id: string, enabled: boolean) => void
  onDeleteScheduledTask: (id: string) => void
  onRunScheduledTask: (id: string) => void
  agentName?: string
  theme: ThemeMode
  onToggleTheme: () => void
}

function groupByDate(conversations: Conversation[]) {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const yesterday = new Date(today)
  yesterday.setDate(yesterday.getDate() - 1)

  const groups: Record<string, Conversation[]> = {
    Today: [],
    Yesterday: [],
    Earlier: [],
  }

  for (const c of conversations) {
    const d = new Date(c.created_at)
    d.setHours(0, 0, 0, 0)
    if (d >= today) groups.Today.push(c)
    else if (d >= yesterday) groups.Yesterday.push(c)
    else groups.Earlier.push(c)
  }

  return groups
}

function formatTimestamp(dateStr: string): string {
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffMins = Math.floor(diffMs / 60_000)
  if (diffMins < 1) return 'just now'
  if (diffMins < 60) return `${diffMins}m ago`
  const diffHours = Math.floor(diffMins / 60)
  if (diffHours < 24) return `${diffHours}h ago`
  const diffDays = Math.floor(diffHours / 24)
  if (diffDays < 7) return `${diffDays}d ago`
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function Sidebar({
  conversations,
  activeId,
  collapsed,
  onToggleCollapsed,
  onSelect,
  onNew,
  onDelete,
  onRename,
  onRefreshConversation,
  onOpenSettings,
  scheduledTasks,
  onOpenScheduledTaskCreate,
  onOpenScheduledTaskEdit,
  onSelectScheduledRun,
  onToggleScheduledTaskEnabled,
  onDeleteScheduledTask,
  onRunScheduledTask,
  agentName = DEFAULT_AGENT_NAME,
  theme,
  onToggleTheme,
}: Props) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const editInputRef = useRef<HTMLInputElement>(null)

  const startEdit = (c: Conversation) => {
    setEditingId(c.id)
    setEditValue(c.title)
    setTimeout(() => editInputRef.current?.select(), 0)
  }

  const commitEdit = (id: string) => {
    const trimmed = editValue.trim()
    if (trimmed && onRename) onRename(id, trimmed)
    setEditingId(null)
  }

  const cancelEdit = () => setEditingId(null)

  const groups = groupByDate(conversations)
  const assistantLabel = resolveAgentName(agentName)
  const ThemeIcon = theme === 'light' ? Moon : Sun
  const nextThemeLabel = theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'

  return (
    <aside
      className={`sidebar-surface relative flex h-full shrink-0 flex-col overflow-hidden border-r border-white/[0.07] bg-[#121110] transition-[width] duration-[220ms] ease-in-out ${
        collapsed ? 'w-14' : 'w-64'
      }`}
    >
      {/* ── Collapsed icon strip ─────────────────────────────── */}
      <div
        className={`absolute inset-0 flex w-14 flex-col items-center px-2 py-3 transition-opacity duration-[160ms] ease-in-out ${
          collapsed ? 'opacity-100 delay-[70ms]' : 'pointer-events-none opacity-0'
        }`}
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          className="ghost-button h-9 w-9 rounded-xl"
          aria-label="Expand sidebar"
          title="Expand sidebar"
        >
          <img src={appLogo} alt="Monaw logo" className="h-8 w-8 object-contain" draggable={false} />
        </button>
        <button
          type="button"
          onClick={onNew}
          className="primary-button mt-2 h-9 w-9 rounded-xl"
          aria-label="New chat"
          title="New chat"
        >
          <Plus size={15} />
        </button>
        <button
          type="button"
          onClick={onToggleCollapsed}
          className="ghost-button mt-2 h-9 w-9 rounded-xl"
          aria-label="Scheduled tasks"
          title="Scheduled tasks"
        >
          <Clock3 size={15} />
        </button>
        <div className="mt-2 flex flex-1 flex-col items-center gap-1 overflow-y-auto">
          {conversations.slice(0, 14).map((c) => {
            const active = c.id === activeId
            return (
              <button
                key={c.id}
                type="button"
                onClick={() => onSelect(c.id)}
                className={`flex h-8 w-9 items-center justify-center rounded-lg border transition-colors ${
                  active
                    ? 'border-accent/20 bg-accent/10 text-accent-light'
                    : 'border-white/[0.06] bg-white/[0.02] text-neutral-600 hover:border-white/[0.1] hover:text-neutral-300'
                }`}
                aria-label={c.title}
                title={c.title}
              >
                <MessageSquare size={13} />
              </button>
            )
          })}
        </div>
        <div className="flex flex-col gap-1">
          <button
            type="button"
            onClick={onToggleTheme}
            className="ghost-button h-9 w-9 rounded-xl"
            aria-label={nextThemeLabel}
            title={nextThemeLabel}
          >
            <ThemeIcon size={15} />
          </button>
          <button
            type="button"
            onClick={onOpenSettings}
            className="ghost-button h-9 w-9 rounded-xl"
            aria-label="Open settings"
            title="Open settings"
          >
            <Settings size={15} />
          </button>
        </div>
      </div>

      {/* ── Expanded full sidebar ─────────────────────────────── */}
      <div
        className={`absolute inset-0 flex w-64 flex-col transition-[opacity,transform] duration-[180ms] ease-out ${
          collapsed
            ? 'pointer-events-none opacity-0'
            : 'opacity-100 delay-[80ms] animate-slide-in-panel'
        }`}
      >
        {/* Header: workspace label + New Chat + collapse toggle */}
        <div className="drag-region border-b border-white/[0.05] px-3 pt-3 pb-2">
          <div className="no-drag flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <img src={appLogo} alt="Monaw logo" className="h-8 w-8 shrink-0 object-contain" draggable={false} />
              <div className="min-w-0">
                <p className="section-label">Workspace</p>
                <h2 className="mt-0.5 max-w-32 truncate text-[13px] font-semibold tracking-tight text-neutral-100">
                  {assistantLabel}
                </h2>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={onNew}
                className="ghost-button h-8 w-8 rounded-lg"
                aria-label="New chat"
                title="New chat"
              >
                <Plus size={15} />
              </button>
              <button
                type="button"
                onClick={onToggleCollapsed}
                className="ghost-button h-8 w-8 rounded-lg"
                aria-label="Collapse sidebar"
                title="Collapse sidebar"
              >
                <ChevronLeft size={15} />
              </button>
            </div>
          </div>
        </div>

        {/* Scheduled tasks + conversation list */}
        <div className="flex-1 overflow-y-auto px-2 py-2">
          <ScheduledTasksList
            tasks={scheduledTasks}
            onCreate={onOpenScheduledTaskCreate}
            onEdit={onOpenScheduledTaskEdit}
            onSelectRun={onSelectScheduledRun}
            onToggleEnabled={onToggleScheduledTaskEnabled}
            onDelete={onDeleteScheduledTask}
            onRunNow={onRunScheduledTask}
          />

          {conversations.length === 0 ? (
            <p className="px-2 py-3 text-[11px] text-neutral-600">
              Conversations will appear here after the first response.
            </p>
          ) : (
            Object.entries(groups).map(([group, convs]) =>
              convs.length === 0 ? null : (
                <div key={group} className="mb-3">
                  <p className="px-2 pb-1 pt-1 text-[9px] font-semibold uppercase tracking-[0.16em] text-neutral-700">
                    {group}
                  </p>
                  <div className="space-y-px">
                    {convs.map((c) => {
                      const active = c.id === activeId
                      const isEditing = editingId === c.id
                      return (
                        <div
                          key={c.id}
                          className={`group relative flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 transition-colors ${
                            active
                              ? 'border border-accent/20 bg-accent/10 text-neutral-100'
                              : 'border border-transparent text-neutral-500 hover:border-white/[0.06] hover:bg-white/[0.03] hover:text-neutral-200'
                          }`}
                          onClick={() => { if (!isEditing) onSelect(c.id) }}
                          onDoubleClick={() => startEdit(c)}
                        >
                          <MessageSquare
                            size={12}
                            className={`shrink-0 ${active ? 'text-accent-light' : 'text-neutral-700'}`}
                          />
                          <div className="min-w-0 flex-1">
                            {isEditing ? (
                              <input
                                ref={editInputRef}
                                value={editValue}
                                onChange={(e) => setEditValue(e.target.value)}
                                onBlur={() => commitEdit(c.id)}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') { e.preventDefault(); commitEdit(c.id) }
                                  if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
                                }}
                                onClick={(e) => e.stopPropagation()}
                                className="w-full rounded bg-white/[0.08] px-1 py-0 text-[11px] leading-snug text-neutral-100 outline-none ring-1 ring-accent/50"
                              />
                            ) : (
                              <>
                                <span className="block truncate text-[11px] leading-snug">{c.title}</span>
                                <span className="block max-h-0 overflow-hidden text-[10px] leading-none text-neutral-700 transition-[max-height] duration-150 group-hover:max-h-4">
                                  {formatTimestamp(c.created_at)}
                                </span>
                              </>
                            )}
                          </div>
                          {!isEditing && (
                            <div className="flex shrink-0 items-center gap-0.5">
                              {active && onRefreshConversation && (
                                <button
                                  type="button"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    onRefreshConversation(c.id)
                                  }}
                                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-neutral-600 transition-all hover:bg-accent/10 hover:text-accent-light focus:outline-none focus:ring-2 focus:ring-accent/20"
                                  aria-label={`Refresh ${c.title}`}
                                  title="Refresh chat"
                                >
                                  <RefreshCw size={11} />
                                </button>
                              )}
                              <button
                                type="button"
                                onClick={(e) => {
                                  e.stopPropagation()
                                  onDelete(c.id)
                                }}
                                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-neutral-700 opacity-0 transition-all hover:bg-red-500/10 hover:text-red-400 group-hover:opacity-100 focus:opacity-100 focus:outline-none"
                                aria-label={`Delete ${c.title}`}
                              >
                                <Trash2 size={11} />
                              </button>
                            </div>
                          )}
                        </div>
                      )
                    })}
                  </div>
                </div>
              ),
            )
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-white/[0.05] p-2">
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={onOpenSettings}
              className="ghost-button h-8 min-w-0 flex-1 justify-start rounded-lg px-2.5 text-[11px]"
            >
              <Settings size={13} />
              Settings
            </button>
            <button
              type="button"
              onClick={onToggleTheme}
              className="ghost-button h-8 w-8 shrink-0 rounded-lg"
              aria-label={nextThemeLabel}
              title={nextThemeLabel}
            >
              <ThemeIcon size={14} />
            </button>
          </div>
        </div>
      </div>
    </aside>
  )
}
