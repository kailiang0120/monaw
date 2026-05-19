import { useState } from 'react'
import { ChevronDown, ChevronRight, Clock3, Plus } from 'lucide-react'
import type { ScheduledTask } from '../../lib/api/types'
import { ScheduledRunHistory } from './ScheduledRunHistory'
import { ScheduledTaskItem } from './ScheduledTaskItem'

const STORAGE_KEY = 'scheduled_section_expanded'

interface Props {
  tasks: ScheduledTask[]
  onCreate: () => void
  onEdit: (id: string) => void
  onSelectRun: (conversationId: string) => void
  onToggleEnabled: (id: string, enabled: boolean) => void
  onDelete: (id: string) => void
  onRunNow: (id: string) => void
}

export function ScheduledTasksList({
  tasks,
  onCreate,
  onEdit,
  onSelectRun,
  onToggleEnabled,
  onDelete,
  onRunNow,
}: Props) {
  const [expanded, setExpanded] = useState(() => {
    if (typeof window === 'undefined') return true
    return window.localStorage.getItem(STORAGE_KEY) !== 'false'
  })
  const [showAll, setShowAll] = useState(false)
  const [historyTask, setHistoryTask] = useState<ScheduledTask | null>(null)
  const visibleTasks = showAll ? tasks : tasks.slice(0, 5)

  const toggleExpanded = () => {
    const next = !expanded
    setExpanded(next)
    window.localStorage.setItem(STORAGE_KEY, String(next))
  }

  return (
    <div className="mb-3 border-b border-white/[0.05] pb-2">
      <div className="flex items-center gap-1 px-2 py-1">
        <button
          type="button"
          onClick={toggleExpanded}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        >
          {expanded ? <ChevronDown size={12} className="text-neutral-600" /> : <ChevronRight size={12} className="text-neutral-600" />}
          <span className="section-label min-w-0 flex-1">Scheduled</span>
          <span className="rounded-full border border-white/[0.08] px-1.5 py-0.5 text-[9px] text-neutral-600">
            {tasks.length}
          </span>
        </button>
        <button
          type="button"
          onClick={onCreate}
          className="ghost-button h-6 w-6 rounded-md"
          aria-label="Create scheduled task"
          title="Create scheduled task"
        >
          <Plus size={12} />
        </button>
      </div>

      {expanded && (
        <div className="space-y-px px-0.5">
          {tasks.length === 0 ? (
            <button
              type="button"
              onClick={onCreate}
              className="mx-1 flex w-[calc(100%-0.5rem)] items-center gap-2 rounded-lg border border-dashed border-white/[0.08] px-2 py-2 text-left text-[11px] text-neutral-600 transition-colors hover:border-accent/30 hover:text-neutral-300"
            >
              <Clock3 size={12} />
              Add a scheduled prompt
            </button>
          ) : (
            visibleTasks.map((task) => (
              <ScheduledTaskItem
                key={task.id}
                task={task}
                onEdit={onEdit}
                onSelectRun={onSelectRun}
                onToggleEnabled={onToggleEnabled}
                onDelete={onDelete}
                onRunNow={onRunNow}
                onViewRuns={setHistoryTask}
              />
            ))
          )}
          {tasks.length > 5 && (
            <button
              type="button"
              onClick={() => setShowAll((value) => !value)}
              className="px-2 py-1 text-[10px] text-neutral-600 transition-colors hover:text-neutral-300"
            >
              {showAll ? 'Show less' : `Show all ${tasks.length}`}
            </button>
          )}
        </div>
      )}

      {historyTask && (
        <ScheduledRunHistory
          task={historyTask}
          onClose={() => setHistoryTask(null)}
          onSelectRun={(conversationId) => {
            setHistoryTask(null)
            onSelectRun(conversationId)
          }}
        />
      )}
    </div>
  )
}
