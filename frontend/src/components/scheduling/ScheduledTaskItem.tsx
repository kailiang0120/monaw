import { useState } from 'react'
import { Clock3, MoreHorizontal, Play, Power, Trash2, type LucideIcon } from 'lucide-react'
import type { ScheduledTask } from '../../lib/api/types'
import { humanizeSchedule, relativeTime } from '../../lib/scheduling/humanize'
import type { ItemActionFeedback } from '../../hooks/useItemActions'

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
        type="button"
        onClick={(event) => {
          event.stopPropagation()
          setMenuOpen((value) => !value)
        }}
        className="absolute right-4 top-1.5 flex h-6 w-6 items-center justify-center rounded-md text-neutral-700 opacity-0 transition-all hover:bg-white/[0.06] hover:text-neutral-200 group-hover:opacity-100 focus:opacity-100"
        aria-label={`Scheduled task actions for ${task.title}`}
      >
        <MoreHorizontal size={13} />
      </button>

      {menuOpen && (
        <div className="panel absolute right-1 top-8 z-20 w-36 overflow-hidden rounded-lg p-1 shadow-xl">
          <MenuButton disabled={Boolean(feedback?.pending)} label="Edit" onClick={() => { setMenuOpen(false); onEdit(task.id) }} />
          <MenuButton disabled={Boolean(feedback?.pending)} icon={Play} label="Run now" onClick={() => { setMenuOpen(false); onRunNow(task.id) }} />
          <MenuButton
            icon={Power}
            label={task.enabled ? 'Disable' : 'Enable'}
            disabled={Boolean(feedback?.pending)}
            onClick={() => { setMenuOpen(false); onToggleEnabled(task.id, !task.enabled) }}
          />
          <MenuButton label="View runs" onClick={() => { setMenuOpen(false); onViewRuns(task) }} />
          <MenuButton disabled={Boolean(feedback?.pending)} icon={Trash2} label="Delete" tone="red" onClick={() => { setMenuOpen(false); onDelete(task.id) }} />
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
      disabled={disabled}
      onClick={onClick}
      className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[11px] transition-colors ${
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
