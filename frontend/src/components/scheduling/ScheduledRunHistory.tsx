import { useEffect, useState } from 'react'
import { ExternalLink, X } from 'lucide-react'
import { fetchScheduledTaskRuns } from '../../lib/api/scheduledTasks'
import type { ScheduledTask, ScheduledTaskRun } from '../../lib/api/types'
import { relativeTime } from '../../lib/scheduling/humanize'

interface Props {
  task: ScheduledTask
  onClose: () => void
  onSelectRun: (conversationId: string) => void
}

export function ScheduledRunHistory({ task, onClose, onSelectRun }: Props) {
  const [runs, setRuns] = useState<ScheduledTaskRun[]>([])

  useEffect(() => {
    let cancelled = false
    fetchScheduledTaskRuns(task.id).then((items) => {
      if (!cancelled) setRuns(items)
    }).catch(() => {
      if (!cancelled) setRuns([])
    })
    return () => {
      cancelled = true
    }
  }, [task.id])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
      <div className="panel w-full max-w-lg overflow-hidden rounded-2xl">
        <div className="flex items-center justify-between border-b border-white/[0.07] px-5 py-4">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold text-neutral-100">{task.title}</h2>
            <p className="text-xs text-neutral-500">Run history</p>
          </div>
          <button type="button" onClick={onClose} className="ghost-button h-8 w-8 rounded-lg" aria-label="Close">
            <X size={15} />
          </button>
        </div>
        <div className="max-h-[420px] overflow-y-auto px-3 py-3">
          {runs.length === 0 ? (
            <p className="px-2 py-8 text-center text-sm text-neutral-500">No runs yet.</p>
          ) : (
            <div className="space-y-1">
              {runs.map((run) => (
                <button
                  key={run.id}
                  type="button"
                  onClick={() => onSelectRun(run.conversationId)}
                  className="flex w-full items-center gap-3 rounded-lg border border-transparent px-3 py-2 text-left transition-colors hover:border-white/[0.08] hover:bg-white/[0.04]"
                >
                  <span className={`h-2 w-2 rounded-full ${statusDot(run.status)}`} />
                  <div className="min-w-0 flex-1">
                    <p className="text-xs font-medium text-neutral-200" title={new Date(run.startedAt).toLocaleString()}>
                      {run.status} · {relativeTime(run.startedAt)}
                    </p>
                    <p className="truncate text-[11px] text-neutral-600">
                      {run.error || run.finalText || run.conversationId}
                    </p>
                  </div>
                  <ExternalLink size={13} className="text-neutral-600" />
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function statusDot(status: string): string {
  if (status === 'ok') return 'bg-emerald-400'
  if (status === 'error') return 'bg-red-400'
  if (status === 'running') return 'bg-amber-300'
  return 'bg-neutral-600'
}
