import { useState } from 'react'
import { Clock3, MoreHorizontal, Play, Power, Trash2, type LucideIcon } from 'lucide-react'
import type { ScheduledTask } from '../../lib/api/types'
import { humanizeSchedule, relativeTime } from '../../lib/scheduling/humanize'

interface Props {
  task: ScheduledTask
  onEdit: (id: string) => void
  onSelectRun: (conversationId: string) => void
  onToggleEnabled: (id: string, enabled: boolean) => void
  onDelete: (id: string) => void
  onRunNow: (id: string) => void
  onViewRuns: (task: ScheduledTask) => void
}

export function ScheduledTaskItem({
  task,
  onEdit,
  onSelectRun,
  onToggleEnabled,
  onDelete,
  onRunNow,
  onViewRuns,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false)
  const schedule = humanizeSchedule(task)
  const next = task.enabled
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
          <MenuButton label="Edit" onClick={() => { setMenuOpen(false); onEdit(task.id) }} />
          <MenuButton icon={Play} label="Run now" onClick={() => { setMenuOpen(false); onRunNow(task.id) }} />
          <MenuButton
            icon={Power}
            label={task.enabled ? 'Disable' : 'Enable'}
            onClick={() => { setMenuOpen(false); onToggleEnabled(task.id, !task.enabled) }}
          />
          <MenuButton label="View runs" onClick={() => { setMenuOpen(false); onViewRuns(task) }} />
          <MenuButton icon={Trash2} label="Delete" tone="red" onClick={() => { setMenuOpen(false); onDelete(task.id) }} />
        </div>
      )}
    </div>
  )
}

function MenuButton({
  icon: Icon,
  label,
  tone = 'neutral',
  onClick,
}: {
  icon?: LucideIcon
  label: string
  tone?: 'neutral' | 'red'
  onClick: () => void
}) {
  return (
    <button
      type="button"
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
  if (!task.enabled) return 'Paused'
  if (task.running) return 'Running now'
  if (task.lastRunStatus === 'ok') return 'Last run completed'
  if (task.lastRunStatus === 'error') return 'Last run failed'
  if (task.lastRunStatus === 'disabled_after_failures') return 'Paused after repeated failures'
  if (task.lastRunStatus === 'skipped') return 'Last run was skipped'
  return 'Has not run yet'
}

function statusDot(task: ScheduledTask): string {
  if (!task.enabled) return 'bg-neutral-600'
  if (task.running) return 'animate-pulse bg-amber-300'
  if (task.lastRunStatus === 'ok') return 'bg-emerald-400'
  if (task.lastRunStatus === 'error' || task.lastRunStatus === 'disabled_after_failures') return 'bg-red-400'
  if (task.lastRunStatus === 'skipped') return 'bg-amber-300'
  return 'bg-neutral-600'
}
