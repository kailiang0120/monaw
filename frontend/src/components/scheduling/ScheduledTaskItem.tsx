import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Clock3, MoreHorizontal, Play, Power, Trash2, type LucideIcon } from 'lucide-react'
import type { ScheduledTask } from '../../lib/api/types'
import { humanizeSchedule, relativeTime } from '../../lib/scheduling/humanize'
import type { ItemActionFeedback } from '../../hooks/useItemActions'
import { InlineConfirm } from '../ui/InlineConfirm'

interface Props {
  task: ScheduledTask
  feedback?: ItemActionFeedback
  onEdit: (id: string) => void
  onSelectRun: (conversationId: string) => void
  onToggleEnabled: (id: string, enabled: boolean) => void
  onDelete: (id: string) => void
  onRunNow: (id: string) => void
  onViewRuns: (task: ScheduledTask) => void
}

export function ScheduledTaskItem({
  task,
  feedback,
  onEdit,
  onSelectRun,
  onToggleEnabled,
  onDelete,
  onRunNow,
  onViewRuns,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const closeMenu = ({ restoreFocus }: { restoreFocus: boolean }) => {
    setMenuOpen(false)
    setConfirmingDelete(false)
    if (restoreFocus) triggerRef.current?.focus()
  }

  const menuItems = () => Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [])

  useEffect(() => {
    if (!menuOpen) return
    menuItems()[0]?.focus()
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return
      setMenuOpen(false)
      setConfirmingDelete(false)
    }
    window.addEventListener('pointerdown', handlePointerDown)
    return () => window.removeEventListener('pointerdown', handlePointerDown)
  }, [menuOpen])

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      closeMenu({ restoreFocus: true })
      return
    }
    if (event.key === 'Tab') {
      closeMenu({ restoreFocus: false })
      return
    }
    const items = menuItems()
    if (items.length === 0) return
    const current = items.indexOf(document.activeElement as HTMLButtonElement)
    const move = (index: number) => {
      event.preventDefault()
      items[(index + items.length) % items.length].focus()
    }
    if (event.key === 'ArrowDown') move(current + 1)
    else if (event.key === 'ArrowUp') move(current < 0 ? items.length - 1 : current - 1)
    else if (event.key === 'Home') move(0)
    else if (event.key === 'End') move(items.length - 1)
  }
  const schedule = humanizeSchedule(task)
  const next = task.queued ? 'waiting to run' : task.enabled
    ? (task.nextRunAt ? `next ${relativeTime(task.nextRunAt)}` : 'no run scheduled')
    : 'paused'

  const openLastRun = () => {
    if (task.lastRunConversationId) onSelectRun(task.lastRunConversationId)
    else onEdit(task.id)
  }

  return (
    <div className="group relative">
      <button
        type="button"
        onClick={openLastRun}
        className="flex w-full items-start gap-2 rounded-lg border border-transparent px-2 py-1.5 text-left text-neutral-500 transition-colors hover:border-white/[0.06] hover:bg-white/[0.03] hover:text-neutral-200"
      >
        <Clock3
          size={12}
          className={`mt-0.5 shrink-0 ${task.enabled ? 'text-accent-light' : 'text-neutral-700'}`}
        />
        <div className="min-w-0 flex-1">
          <span className="block truncate text-[11px] leading-snug text-neutral-200">{task.title}</span>
          <span className="block truncate text-[10px] leading-snug text-neutral-600">
            {schedule} · {next}
          </span>
        </div>
        <span
          className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${statusDot(task)}`}
          title={statusLabel(task)}
        />
        <span className="sr-only">{statusLabel(task)}</span>
      </button>

      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={(event) => {
          event.stopPropagation()
          if (menuOpen) closeMenu({ restoreFocus: false })
          else setMenuOpen(true)
        }}
        className="absolute right-4 top-1.5 flex h-6 w-6 items-center justify-center rounded-md text-neutral-700 opacity-0 transition-all hover:bg-white/[0.06] hover:text-neutral-200 group-hover:opacity-100 focus:opacity-100"
        aria-label={`Scheduled task actions for ${task.title}`}
      >
        <MoreHorizontal size={13} />
      </button>

      {menuOpen && (
        <div
          ref={menuRef}
          role="menu"
          aria-label={`Actions for ${task.title}`}
          onKeyDown={handleMenuKeyDown}
          className="panel absolute right-1 top-8 z-20 w-36 overflow-hidden rounded-lg p-1 shadow-xl"
        >
          {confirmingDelete ? (
            <div className="flex items-center justify-between gap-1 px-1 py-1">
              <span className="text-[11px] text-neutral-300" aria-hidden="true">Delete?</span>
              <InlineConfirm
                label={`Delete "${task.title}"?`}
                onConfirm={() => { closeMenu({ restoreFocus: false }); onDelete(task.id) }}
                onCancel={() => closeMenu({ restoreFocus: true })}
              />
            </div>
          ) : (
            <>
              <MenuButton disabled={Boolean(feedback?.pending)} label="Edit" onClick={() => { closeMenu({ restoreFocus: false }); onEdit(task.id) }} />
              <MenuButton disabled={Boolean(feedback?.pending)} icon={Play} label="Run now" onClick={() => { closeMenu({ restoreFocus: true }); onRunNow(task.id) }} />
              <MenuButton
                icon={Power}
                label={task.enabled ? 'Disable' : 'Enable'}
                disabled={Boolean(feedback?.pending)}
                onClick={() => { closeMenu({ restoreFocus: true }); onToggleEnabled(task.id, !task.enabled) }}
              />
              <MenuButton label="View runs" onClick={() => { closeMenu({ restoreFocus: false }); onViewRuns(task) }} />
              <MenuButton disabled={Boolean(feedback?.pending)} icon={Trash2} label="Delete" tone="red" onClick={() => setConfirmingDelete(true)} />
            </>
          )}
        </div>
      )}
      {feedback?.pending && <div role="status" className="px-2 text-[10px] text-neutral-400">{feedback.pending}…</div>}
      {feedback?.error && (
        <div role="alert" className="px-2 py-1 text-[10px] text-red-300">
          {feedback.error}
          {feedback.retry && <button type="button" className="ml-1 underline" onClick={feedback.retry}>{feedback.retryLabel || 'Retry'}</button>}
        </div>
      )}
      {feedback?.success && <div role="status" className="px-2 text-[10px] text-neutral-500">{feedback.success}</div>}
    </div>
  )
}

function MenuButton({
  icon: Icon,
  label,
  tone = 'neutral',
  onClick,
  disabled,
}: {
  icon?: LucideIcon
  label: string
  tone?: 'neutral' | 'red'
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="menuitem"
      tabIndex={-1}
      disabled={disabled}
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[11px] transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30 ${
        tone === 'red'
          ? 'text-red-300 hover:bg-red-500/10'
          : 'text-neutral-300 hover:bg-white/[0.06] hover:text-neutral-100'
      }`}
    >
      {Icon && <Icon size={12} />}
      {label}
    </button>
  )
}

function statusLabel(task: ScheduledTask): string {
  if (task.running) return 'Running now'
  if (task.queued) return 'Waiting to run'
  if (task.lastRunStatus === 'dead_letter') return 'Paused after repeated failures'
  if (!task.enabled) return 'Paused'
  if (task.lastRunStatus === 'ok') return 'Last run completed'
  if (task.lastRunStatus === 'error') return 'Last run failed'
  if (task.lastRunStatus === 'disabled_after_failures') return 'Paused after repeated failures'
  if (['skipped', 'overlap', 'queue_limit', 'global_concurrency_limit'].includes(task.lastRunStatus)) return 'Last run was skipped'
  return 'Has not run yet'
}

function statusDot(task: ScheduledTask): string {
  if (task.running) return 'animate-pulse bg-amber-300'
  if (task.queued) return 'bg-amber-300'
  if (!task.enabled) return 'bg-neutral-600'
  if (task.lastRunStatus === 'ok') return 'bg-emerald-400'
  if (task.lastRunStatus === 'error' || task.lastRunStatus === 'disabled_after_failures') return 'bg-red-400'
  if (['skipped', 'overlap', 'queue_limit', 'global_concurrency_limit'].includes(task.lastRunStatus)) return 'bg-amber-300'
  return 'bg-neutral-600'
}
