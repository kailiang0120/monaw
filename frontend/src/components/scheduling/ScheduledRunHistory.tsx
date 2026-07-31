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
    <div className="st-overlay fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Run history for ${task.title}`}
        className="settings-shell w-full max-w-lg overflow-hidden rounded-2xl"
      >
        <header className="st-divider-b flex items-center justify-between gap-3 px-5 py-4">
          <div className="min-w-0">
            <h2 className="st-title truncate">{task.title}</h2>
            <p className="st-desc mt-0.5">Every time this task has run. Select one to open its conversation.</p>
          </div>
          <button type="button" onClick={onClose} className="st-btn st-btn-ghost st-btn-icon shrink-0" aria-label="Close">
            <X size={15} />
          </button>
        </header>
        <div className="st-scroll max-h-[420px] overflow-y-auto p-3">
          {runs.length === 0 ? (
            <p className="st-desc px-2 py-10 text-center">
              This task has not run yet.
            </p>
          ) : (
            <div className="space-y-1">
              {runs.map((run) => (
                <button
                  key={run.id}
                  type="button"
                  onClick={() => onSelectRun(run.conversationId)}
                  className="st-nav-item items-start"
                >
                  <span className={`${statusDot(run.status)} mt-1.5`} />
                  <span className="min-w-0 flex-1">
                    <span className="st-label block" title={new Date(run.startedAt).toLocaleString()}>
                      {describeStatus(run.status)} · {relativeTime(run.startedAt)}
                    </span>
                    <span className="st-hint block truncate">
                      {run.error || run.finalText || run.conversationId}
                    </span>
                  </span>
                  <ExternalLink size={13} className="st-nav-icon mt-1" />
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
  if (status === 'ok') return 'st-dot st-dot-ok'
  if (status === 'error') return 'st-dot st-dot-danger'
  if (status === 'running') return 'st-dot st-dot-warn'
  return 'st-dot'
}

function describeStatus(status: string): string {
  if (status === 'ok') return 'Completed'
  if (status === 'error') return 'Failed'
  if (status === 'running') return 'Running'
  if (status === 'skipped') return 'Skipped'
  return status
}
