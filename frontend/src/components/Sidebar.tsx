import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, Clock3, Folder, Loader2, MessageSquare, Moon, Plus, RefreshCw, Settings, SquarePen, Sun, Trash2 } from 'lucide-react'
import type { Conversation, ScheduledTask } from '../lib/api/types'
import { ScheduledTasksList } from './scheduling/ScheduledTasksList'
import { InlineConfirm } from './ui/InlineConfirm'
import type { ItemActionFeedback } from '../hooks/useItemActions'

type ThemeMode = 'dark' | 'light'

interface Props {
  conversations: Conversation[]
  activeId: string | null
  collapsed: boolean
  onToggleCollapsed: () => void
  // Here rather than in the title bar, away from the window buttons.
  onNewChat: (workspaceDir?: string) => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onRename?: (id: string, title: string) => void | Promise<boolean>
  onRefreshConversation?: (id: string) => void
  onOpenSettings: () => void
  scheduledTasks: ScheduledTask[]
  actionFeedback?: Record<string, ItemActionFeedback>
  onOpenScheduledTaskCreate: () => void
  onOpenScheduledTaskEdit: (id: string) => void
  onSelectScheduledRun: (conversationId: string) => void
  onToggleScheduledTaskEnabled: (id: string, enabled: boolean) => void
  onDeleteScheduledTask: (id: string) => void
  onRunScheduledTask: (id: string) => void
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

const WORKSPACE_GROUP_PREVIEW = 5
const FOLDED_WORKSPACES_KEY = 'monaw.sidebar.foldedWorkspaces'

interface WorkspaceGroup {
  dir: string
  label: string
  chats: Conversation[]
}

function workspaceLabel(dir: string): string {
  if (!dir) return 'Default workspace'
  const parts = dir.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || dir
}

/** Chats by working folder, the most recently active folder first. */
function groupByWorkspace(conversations: Conversation[]): WorkspaceGroup[] {
  const groups = new Map<string, WorkspaceGroup & { latest: string }>()
  for (const c of conversations) {
    const dir = c.workspace_dir || ''
    const group = groups.get(dir) ?? { dir, label: workspaceLabel(dir), chats: [], latest: '' }
    group.chats.push(c)
    const activity = c.updated_at || c.created_at
    if (activity > group.latest) group.latest = activity
    groups.set(dir, group)
  }
  return [...groups.values()].sort((a, b) => b.latest.localeCompare(a.latest))
}

// Which groups are folded is a per-window convenience; it may be unavailable.
function loadFoldedWorkspaces(): Set<string> {
  try {
    const saved = JSON.parse(window.localStorage.getItem(FOLDED_WORKSPACES_KEY) || '[]')
    return new Set(Array.isArray(saved) ? saved.filter((item): item is string => typeof item === 'string') : [])
  } catch {
    return new Set()
  }
}

function saveFoldedWorkspaces(folded: Set<string>) {
  try {
    window.localStorage.setItem(FOLDED_WORKSPACES_KEY, JSON.stringify([...folded]))
  } catch {
    // Folding still works for this session.
  }
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
  onNewChat,
  onSelect,
  onDelete,
  onRename,
  onRefreshConversation,
  onOpenSettings,
  scheduledTasks,
  actionFeedback,
  onOpenScheduledTaskCreate,
  onOpenScheduledTaskEdit,
  onSelectScheduledRun,
  onToggleScheduledTaskEnabled,
  onDeleteScheduledTask,
  onRunScheduledTask,
  theme,
  onToggleTheme,
}: Props) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const editInputRef = useRef<HTMLInputElement>(null)
  const renaming = useRef(new Set<string>())
  const editGeneration = useRef(0)

  const startEdit = (c: Conversation) => {
    editGeneration.current += 1
    setEditingId(c.id)
    setEditValue(c.title)
    setTimeout(() => editInputRef.current?.select(), 0)
  }

  const commitEdit = async (id: string) => {
    const trimmed = editValue.trim()
    if (renaming.current.has(id)) return
    if (!trimmed || !onRename) { setEditingId(null); return }
    renaming.current.add(id)
    const generation = editGeneration.current
    try {
      const saved = await onRename(id, trimmed)
      if (saved !== false && generation === editGeneration.current) setEditingId((current) => current === id ? null : current)
    } finally {
      renaming.current.delete(id)
    }
  }

  const cancelEdit = () => { editGeneration.current += 1; setEditingId(null) }

  // Delete asks first; keyboard focus follows the row through rename and the delete prompt.
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<string | null>(null)
  const rowButtons = useRef(new Map<string, HTMLButtonElement>())
  const deleteButtons = useRef(new Map<string, HTMLButtonElement>())
  const pendingFocus = useRef<{ id: string; target: 'row' | 'delete' } | null>(null)
  const setRowButton = (id: string, element: HTMLButtonElement | null) => {
    if (element) rowButtons.current.set(id, element)
    else rowButtons.current.delete(id)
  }
  const setDeleteButton = (id: string, element: HTMLButtonElement | null) => {
    if (element) deleteButtons.current.set(id, element)
    else deleteButtons.current.delete(id)
  }
  const focusAfterRender = (id: string, target: 'row' | 'delete') => { pendingFocus.current = { id, target } }
  useEffect(() => {
    const pending = pendingFocus.current
    if (!pending) return
    const element = (pending.target === 'row' ? rowButtons : deleteButtons).current.get(pending.id)
    if (!element) return
    pendingFocus.current = null
    element.focus()
  })

  const renderConversation = (c: Conversation) => {
    const active = c.id === activeId
    const isEditing = editingId === c.id
    const feedback = actionFeedback?.[`conversation:${c.id}`]
    return (
      <div
        key={c.id}
        className={`group relative rounded-lg px-2 py-1.5 transition-colors ${
          active
            ? 'border border-accent/20 bg-accent/10 text-neutral-100'
            : 'border border-transparent text-neutral-500 hover:border-white/[0.06] hover:bg-white/[0.03] hover:text-neutral-200'
        }`}
      >
        <div className="flex items-center gap-2">
          {isEditing ? (
            <>
              <MessageSquare size={12} className={`shrink-0 ${active ? 'text-accent-light' : 'text-neutral-700'}`} />
              <input
                ref={editInputRef}
                value={editValue}
                disabled={Boolean(feedback?.pending)}
                aria-label={`Rename ${c.title}`}
                onChange={(e) => setEditValue(e.target.value)}
                onBlur={() => commitEdit(c.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') { e.preventDefault(); focusAfterRender(c.id, 'row'); void commitEdit(c.id) }
                  if (e.key === 'Escape') { e.preventDefault(); focusAfterRender(c.id, 'row'); cancelEdit() }
                }}
                className="min-w-0 flex-1 rounded bg-white/[0.08] px-1 py-0 text-[11px] leading-snug text-neutral-100 outline-none ring-1 ring-accent/50"
              />
            </>
          ) : (
            <button
              ref={(element) => setRowButton(c.id, element)}
              type="button"
              aria-current={active ? 'page' : undefined}
              aria-keyshortcuts={onRename ? 'F2' : undefined}
              title={onRename ? 'Double-click or press F2 to rename' : undefined}
              onClick={() => onSelect(c.id)}
              onDoubleClick={() => startEdit(c)}
              onKeyDown={(e) => {
                if (e.key === 'F2' && onRename) { e.preventDefault(); startEdit(c) }
              }}
              className="flex min-w-0 flex-1 items-center gap-2 rounded-md text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            >
              {c.running
                ? <Loader2 size={12} aria-label="Running" className="shrink-0 animate-spin text-accent-light" />
                : <MessageSquare size={12} className={`shrink-0 ${active ? 'text-accent-light' : 'text-neutral-700'}`} />}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[11px] leading-snug">{c.title}</span>
                <span className="block max-h-0 overflow-hidden text-[10px] leading-none text-neutral-700 transition-[max-height] duration-150 group-hover:max-h-4 group-focus-within:max-h-4">
                  {formatTimestamp(c.created_at)}
                </span>
              </span>
            </button>
          )}
          {!isEditing && (confirmingDeleteId === c.id ? (
            <InlineConfirm
              label={`Delete "${c.title}"?`}
              onConfirm={() => { setConfirmingDeleteId(null); onDelete(c.id) }}
              onCancel={() => { focusAfterRender(c.id, 'delete'); setConfirmingDeleteId(null) }}
            />
          ) : (
            <div className="flex shrink-0 items-center gap-0.5">
              {active && onRefreshConversation && (
                <button
                  type="button"
                  onClick={() => onRefreshConversation(c.id)}
                  className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-neutral-600 transition-all hover:bg-accent/10 hover:text-accent-light focus:outline-none focus:ring-2 focus:ring-accent/20"
                  aria-label={`Refresh ${c.title}`}
                  title="Refresh chat"
                >
                  <RefreshCw size={11} />
                </button>
              )}
              <button
                ref={(element) => setDeleteButton(c.id, element)}
                type="button"
                onClick={() => setConfirmingDeleteId(c.id)}
                className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-neutral-700 opacity-0 transition-all hover:bg-red-500/10 hover:text-red-400 group-hover:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400/30"
                aria-label={`Delete ${c.title}`}
                disabled={Boolean(feedback?.pending)}
              >
                <Trash2 size={11} />
              </button>
            </div>
          ))}
        </div>
        {feedback?.pending && <span role="status" className="block pl-5 text-[10px]">{feedback.pending}…</span>}
        {feedback?.error && (
          <div role="alert" className="mt-1 pl-5 text-[10px] text-red-300">
            {feedback.error}
            {feedback.retry && <button type="button" onClick={() => feedback.retry?.()} className="ml-1 underline">Retry</button>}
          </div>
        )}
        {feedback?.success && <span role="status" className="block pl-5 text-[10px] text-neutral-500">{feedback.success}</span>}
      </div>
    )
  }

  const groups = groupByDate(conversations)
  // With chats in two or more workspaces, the list groups by workspace instead of by date.
  const workspaceGroups = groupByWorkspace(conversations)
  const [foldedWorkspaces, setFoldedWorkspaces] = useState<Set<string>>(loadFoldedWorkspaces)
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<Set<string>>(new Set())
  const toggleFolded = (dir: string) => setFoldedWorkspaces((current) => {
    const next = new Set(current)
    if (!next.delete(dir)) next.add(dir)
    saveFoldedWorkspaces(next)
    return next
  })
  const toggleExpanded = (dir: string) => setExpandedWorkspaces((current) => {
    const next = new Set(current)
    if (!next.delete(dir)) next.add(dir)
    return next
  })
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
          aria-label="Scheduled tasks"
          title="Scheduled tasks"
        >
          <Clock3 size={15} />
        </button>
        <button
          type="button"
          onClick={() => onNewChat()}
          className="ghost-button mt-1 h-9 w-9 rounded-xl"
          aria-label="New chat"
          title="New chat"
          tabIndex={collapsed ? 0 : -1}
        >
          <SquarePen size={15} />
        </button>
        <div className="sidebar-scroll mt-2 flex flex-1 flex-col items-center gap-1 overflow-y-auto">
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
                aria-label={c.running ? `${c.title} (running)` : c.title}
                title={c.running ? `${c.title} · running` : c.title}
              >
                {c.running ? <Loader2 size={13} className="animate-spin text-accent-light" /> : <MessageSquare size={13} />}
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
        {/* Scheduled stays put; only the chat list below scrolls. */}
        <div className="sidebar-scroll max-h-[40%] shrink-0 overflow-y-auto px-2 pt-2">
          <ScheduledTasksList
            tasks={scheduledTasks}
            actionFeedback={actionFeedback}
            onCreate={onOpenScheduledTaskCreate}
            onEdit={onOpenScheduledTaskEdit}
            onSelectRun={onSelectScheduledRun}
            onToggleEnabled={onToggleScheduledTaskEnabled}
            onDelete={onDeleteScheduledTask}
            onRunNow={onRunScheduledTask}
          />
        </div>
        <div className="shrink-0 px-2 py-2">
          <button
            type="button"
            onClick={() => onNewChat()}
            className="flex w-full items-center gap-2 rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-[13px] font-medium text-neutral-200 transition-colors hover:border-accent/30 hover:bg-accent/10 hover:text-accent-light"
            aria-label="New chat"
            tabIndex={collapsed ? -1 : 0}
          >
            <SquarePen size={14} />
            New chat
          </button>
        </div>
        <div className="sidebar-scroll min-h-0 flex-1 overflow-y-auto px-2 pb-2">

          {conversations.length === 0 ? (
            <p className="px-2 py-3 text-[11px] text-neutral-600">
              Conversations will appear here after the first response.
            </p>
          ) : workspaceGroups.length > 1 ? (
            workspaceGroups.map((group) => {
              const folded = foldedWorkspaces.has(group.dir)
              const showAll = expandedWorkspaces.has(group.dir)
              // A group shows its newest chats; the open chat and running ones always show.
              const visible = showAll ? group.chats : group.chats.filter(
                (c, index) => index < WORKSPACE_GROUP_PREVIEW || c.id === activeId || c.running,
              )
              const hidden = group.chats.length - visible.length
              return (
                <div key={group.dir || 'default'} className="mb-2">
                  <div className="group/ws flex items-center gap-1 pr-1">
                    <button
                      type="button"
                      onClick={() => toggleFolded(group.dir)}
                      aria-expanded={!folded}
                      title={group.dir || 'The default workspace folder'}
                      className="flex min-w-0 flex-1 items-center gap-1 rounded-md px-1 py-1 text-left text-[10px] font-semibold uppercase tracking-[0.12em] text-neutral-600 hover:text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                    >
                      {folded ? <ChevronRight size={11} className="shrink-0" /> : <ChevronDown size={11} className="shrink-0" />}
                      {group.chats.some((c) => c.running) ? <Loader2 size={11} className="shrink-0 animate-spin text-accent-light" /> : <Folder size={11} className="shrink-0" />}
                      <span className="truncate">{group.label}</span>
                      <span className="shrink-0 font-normal text-neutral-700">{group.chats.length}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => onNewChat(group.dir)}
                      className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-neutral-600 opacity-0 transition-all hover:bg-accent/10 hover:text-accent-light group-hover/ws:opacity-100 focus:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                      aria-label={`New chat in ${group.label}`}
                      title={`New chat in ${group.label}`}
                    >
                      <Plus size={12} />
                    </button>
                  </div>
                  {!folded && (
                    <div className="space-y-px">
                      {visible.map(renderConversation)}
                      {(hidden > 0 || showAll) && group.chats.length > WORKSPACE_GROUP_PREVIEW && (
                        <button
                          type="button"
                          onClick={() => toggleExpanded(group.dir)}
                          className="w-full rounded-md px-2 py-1 text-left text-[10px] text-neutral-600 hover:text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                        >
                          {showAll ? 'Show fewer' : `Show ${hidden} more`}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              )
            })
          ) : (
            Object.entries(groups).map(([group, convs]) =>
              convs.length === 0 ? null : (
                <div key={group} className="mb-3">
                  <p className="px-2 pb-1 pt-1 text-[9px] font-semibold uppercase tracking-[0.16em] text-neutral-700">
                    {group}
                  </p>
                  <div className="space-y-px">
                    {convs.map(renderConversation)}
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
