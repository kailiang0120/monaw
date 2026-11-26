import { useEffect, useRef, useState } from 'react'
import { ExternalLink, X } from 'lucide-react'
import { useModalFocus } from '../../hooks/useModalFocus'
import { fetchScheduledTaskRuns, retryScheduledNotification } from '../../lib/api/scheduledTasks'
import type { ScheduledTask, ScheduledTaskRun } from '../../lib/api/types'
import { relativeTime } from '../../lib/scheduling/humanize'

interface Props {
  task: ScheduledTask
  onClose: () => void
  onSelectRun: (conversationId: string) => void
}

export function ScheduledRunHistory({ task, onClose, onSelectRun }: Props) {
  const [runs, setRuns] = useState<ScheduledTaskRun[]>([])
  const [retrying, setRetrying] = useState<number | null>(null)
  const [deliveryError, setDeliveryError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  const dialogRef = useRef<HTMLDivElement>(null)
  useModalFocus(dialogRef, { onEscape: onClose })

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    /** Refresh delivery state while a result is waiting to be sent. */
    const load = () => fetchScheduledTaskRuns(task.id).then((items) => {
      if (cancelled) return
      setRuns(items)
      if (items.some((run) => ['pending', 'sending'].includes(run.notificationStatus ?? ''))) {
        timer = window.setTimeout(() => void load(), 3000)
      }
    }).catch(() => { if (!cancelled) setRuns([]) })
    void load()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [task.id, refreshKey])

  /** Queue another delivery attempt while retaining the original run result. */
  const retryDelivery = async (runId: number) => {
    setRetrying(runId)
    setDeliveryError('')
    try {
      await retryScheduledNotification(task.id, runId)
      setRefreshKey((value) => value + 1)
    } catch (caught) {
      setDeliveryError(caught instanceof Error ? caught.message : 'Could not retry delivery.')
    } finally {
      setRetrying(null)
    }
  }

  return (
    <div className="st-overlay fixed inset-0 below-app-bar z-50 flex items-center justify-center p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Run history for ${task.title}`}
        tabIndex={-1}
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
          {deliveryError && <p role="alert" className="px-2 py-2 text-sm text-red-300">{deliveryError}</p>}
          {runs.length === 0 ? (
            <p className="st-desc px-2 py-10 text-center">
              This task has not run yet.
            </p>
          ) : (
            <div className="space-y-1">
              {runs.map((run) => (
                <div key={run.id}>
                  <button
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
                      {run.notificationStatus && run.notificationStatus !== 'not_requested' && (
                        <span className="st-hint block">Telegram: {run.notificationStatus}</span>
                      )}
                    </span>
                    <ExternalLink size={13} className="st-nav-icon mt-1" />
                  </button>
                  {['failed', 'blocked'].includes(run.notificationStatus ?? '') && (
                    <button type="button" className="st-btn st-btn-ghost ml-6 min-h-8 text-xs" disabled={retrying !== null} onClick={() => void retryDelivery(run.id)}>
                      {retrying === run.id ? 'Queuing…' : 'Retry notification'}
                    </button>
                  )}
                </div>
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
