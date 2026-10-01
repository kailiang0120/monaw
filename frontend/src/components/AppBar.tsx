import { useRef, useState } from 'react'
import { PanelLeftClose, PanelLeftOpen, ShieldAlert, ShieldCheck, ShieldHalf, SquarePen } from 'lucide-react'
import type { AgentSettings } from '../lib/api/types'
import { resolveAgentName } from '../lib/identity'
import appLogo from '../assets/Logo.png'

type ApprovalMode = AgentSettings['permissions']['mode']
export type AgentStatus = 'starting' | 'ready' | 'working'

interface Props {
  sidebarCollapsed: boolean
  onToggleSidebar: () => void
  onNewChat: () => void
  agentName?: string
  conversationTitle?: string
  onRenameConversation?: (title: string) => void
  status: AgentStatus
  approvalMode: ApprovalMode
  onOpenPermissions?: () => void
}

const STATUS_LABEL: Record<AgentStatus, string> = {
  starting: 'Starting',
  ready: 'Ready',
  working: 'Working',
}

// The permission level is always on screen so a user never has to wonder how
// much the agent may do without asking.
const MODE_META: Record<ApprovalMode, { label: string; hint: string; Icon: typeof ShieldCheck }> = {
  default: { label: 'Default', hint: 'Routine actions run automatically; file changes and commands ask.', Icon: ShieldCheck },
  auto_review: { label: 'Auto Review', hint: 'AI reviews changes; uncertain actions ask you.', Icon: ShieldHalf },
  custom: { label: 'Custom', hint: 'Custom: your permission rules decide what needs approval.', Icon: ShieldHalf },
  full_access: { label: 'Full access', hint: 'Full access: most actions run without asking.', Icon: ShieldAlert },
}

function platformClass(): string {
  const platform = typeof window !== 'undefined' ? window.electronAPI?.platform : undefined
  if (platform === 'darwin') return 'app-bar--mac'
  if (platform) return 'app-bar--caption'
  return ''
}

export function AppBar({
  sidebarCollapsed,
  onToggleSidebar,
  onNewChat,
  agentName,
  conversationTitle,
  onRenameConversation,
  status,
  approvalMode,
  onOpenPermissions,
}: Props) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const canRename = Boolean(conversationTitle && onRenameConversation)
  const mode = MODE_META[approvalMode] ?? MODE_META.default
  const ModeIcon = mode.Icon
  const ToggleIcon = sidebarCollapsed ? PanelLeftOpen : PanelLeftClose
  const toggleLabel = sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'

  const startEdit = () => {
    if (!canRename) return
    setDraft(conversationTitle || '')
    setEditing(true)
    setTimeout(() => inputRef.current?.select(), 0)
  }

  const commitEdit = () => {
    const trimmed = draft.trim()
    if (trimmed && trimmed !== conversationTitle) onRenameConversation?.(trimmed)
    setEditing(false)
  }

  return (
    <header
      className={`app-bar drag-region ${platformClass()} ${sidebarCollapsed ? 'app-bar--collapsed' : ''}`}
      data-status={status}
    >
      <div className="app-bar-cap">
        {sidebarCollapsed ? (
          <button
            type="button"
            onClick={onToggleSidebar}
            className="no-drag app-bar-icon-button"
            aria-label={toggleLabel}
            title={toggleLabel}
          >
            <img src={appLogo} alt="" className="h-6 w-6 object-contain" draggable={false} />
          </button>
        ) : (
          <>
            <div className="app-bar-wordmark">
              <img src={appLogo} alt="Monaw logo" className="h-6 w-6 shrink-0 object-contain" draggable={false} />
              <span className="truncate">{resolveAgentName(agentName)}</span>
            </div>
            <button
              type="button"
              onClick={onToggleSidebar}
              className="no-drag app-bar-icon-button"
              aria-label={toggleLabel}
              title={toggleLabel}
            >
              <ToggleIcon size={15} />
            </button>
          </>
        )}
      </div>

      <div className="app-bar-center">
        <div className="app-bar-capsule no-drag" role="group" aria-label="Current session">
          <span className="app-bar-status" aria-live="polite">
            <span className="app-bar-status-dot" aria-hidden="true" />
            {STATUS_LABEL[status]}
          </span>
          <span className="app-bar-divider" aria-hidden="true" />
          {editing ? (
            <input
              ref={inputRef}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onBlur={commitEdit}
              onKeyDown={(event) => {
                if (event.key === 'Enter') { event.preventDefault(); commitEdit() }
                if (event.key === 'Escape') { event.preventDefault(); setEditing(false) }
              }}
              className="app-bar-title-input"
              aria-label="Conversation title"
            />
          ) : (
            <h1
              className={`app-bar-title ${canRename ? 'app-bar-title--editable' : ''}`}
              title={canRename ? 'Click to rename' : undefined}
              onClick={startEdit}
            >
              {conversationTitle || 'New chat'}
            </h1>
          )}
          <span className="app-bar-divider" aria-hidden="true" />
          <button
            type="button"
            onClick={onOpenPermissions}
            className={`app-bar-mode app-bar-mode--${approvalMode}`}
            title={mode.hint}
            aria-label={`Permission mode: ${mode.label}`}
          >
            <ModeIcon size={12} />
            {mode.label}
          </button>
        </div>
      </div>

      <div className="app-bar-actions">
        <button
          type="button"
          onClick={onNewChat}
          className="no-drag app-bar-new"
          aria-label="New chat"
          title="New chat"
        >
          <SquarePen size={14} />
          <span>New chat</span>
        </button>
      </div>

    </header>
  )
}
