import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, Pause, Play, Square } from 'lucide-react'
import type { OperationResolution, RecoveryReport, RunAction as ApiRunAction, RunEvent, RunOperation, RunSnapshot } from '../lib/api/runs'
import type { FileMutationUndoResult, UndoCapability } from '../lib/api/fileMutations'
import type { AgentSettings } from '../lib/api/types'
import { ApiError } from '../lib/api/client'
import { Dropdown } from './Dropdown'
import { RecoveryPanel } from './RecoveryPanel'
import { BrowserVerificationStatus } from './BrowserVerificationStatus'
import { browserOperationOutcome } from '../lib/browserAction'

export type RunAction = Extract<ApiRunAction, 'pause' | 'resume' | 'stop'>

export interface ExecutionTimelineProps {
  runs?: RunSnapshot[]
  snapshot?: RunSnapshot | null
  operations?: RunOperation[]
  events?: RunEvent[]
  recovery?: RecoveryReport | null
  detailMode?: AgentSettings['execution_detail']
  loading?: boolean
  error?: string | null
  connected?: boolean
  busy?: boolean
  onSelectRun?: (runId: string) => void
  onAction?: (action: RunAction) => void | Promise<void>
  onResolveOperation?: (operationId: string, resolution: OperationResolution) => void | Promise<void>
  onUndo?: (operation: RunOperation, capability: UndoCapability, confirmationId: string) => Promise<FileMutationUndoResult>
  onRefresh?: () => void | Promise<void>
}

const RUN_STATUS_LABELS: Record<RunSnapshot['status'], string> = {
  running: 'Running',
  waiting_approval: 'Waiting for approval',
  pausing: 'Pausing safely',
  paused: 'Paused',
  interrupted: 'Interrupted',
  needs_review: 'Needs review',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Stopped',
}

const OPERATION_STATUS_LABELS: Record<RunOperation['status'], string> = {
  prepared: 'Prepared',
  authorized: 'Authorized',
  executing: 'In progress',
  verified: 'Verified',
  failed: 'Failed',
  cancelled: 'Cancelled',
  uncertain: 'Uncertain',
  needs_review: 'Needs review',
  returned: 'Returned; effect not verified',
}

const REASON_LABELS: Record<string, string> = {
  external_effect_unknown: 'An action may have taken effect, but its result could not be confirmed.',
  worker_lost: 'The worker stopped before it recorded a final result.',
  user_pause: 'Paused at a safe boundary.',
  user_stop: 'Stopped at the user’s request.',
  undo_conflict: 'The file changed after the action, so Undo was not applied.',
  verified: 'The file change was verified.',
  verification_failed: 'The action could not be verified.',
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (letter) => letter.toUpperCase())
}

function reasonText(reasonCode?: string | null): string | undefined {
  if (!reasonCode) return undefined
  return REASON_LABELS[reasonCode] ?? humanize(reasonCode)
}

function actionLabel(action: RunAction): string {
  if (action === 'pause') return 'Pause'
  if (action === 'resume') return 'Continue'
  return 'Stop'
}

function actionProgressLabel(action: RunAction): string {
  if (action === 'pause') return 'Pausing…'
  if (action === 'resume') return 'Continuing…'
  return 'Stopping…'
}

function ActionIcon({ action }: { action: RunAction }) {
  if (action === 'pause') return <Pause size={13} aria-hidden="true" />
  if (action === 'resume') return <Play size={13} aria-hidden="true" />
  return <Square size={12} aria-hidden="true" />
}

function OperationIcon({ status }: { status: RunOperation['status'] }) {
  if (status === 'verified') return <CheckCircle2 size={14} className="text-emerald-300" aria-hidden="true" />
  if (status === 'executing' || status === 'prepared' || status === 'authorized') {
    return <CircleDashed size={14} className="animate-spin text-accent-light" aria-hidden="true" />
  }
  if (status === 'uncertain' || status === 'needs_review') {
    return <AlertTriangle size={14} className="text-amber-300" aria-hidden="true" />
  }
  return <CheckCircle2 size={14} className="text-neutral-600" aria-hidden="true" />
}

function formatRunChoice(run: RunSnapshot): string {
  return `Attempt ${run.attempt} · ${RUN_STATUS_LABELS[run.status]}`
}

function formatRunTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function ExecutionTimeline({
  runs = [],
  snapshot,
  operations = [],
  events = [],
  recovery,
  detailMode = 'simple',
  loading = false,
  error,
  connected,
  busy = false,
  onSelectRun,
  onAction,
  onResolveOperation,
  onUndo,
  onRefresh,
}: ExecutionTimelineProps) {
  const [pendingAction, setPendingAction] = useState<RunAction | null>(null)
  const [actionError, setActionError] = useState('')
  const [operationsExpanded, setOperationsExpanded] = useState(detailMode !== 'simple')
  const detailContextRef = useRef({ mode: detailMode, runId: snapshot?.run_id ?? null })

  useEffect(() => {
    const previous = detailContextRef.current
    const runChanged = previous.runId !== (snapshot?.run_id ?? null)
    const modeChanged = previous.mode !== detailMode
    if (runChanged) {
      // Each Run starts at the chosen mode's default: compact by default,
      // expanded for the two detail modes.
      setOperationsExpanded(detailMode !== 'simple')
    } else if (modeChanged && detailMode !== 'simple') {
      // Entering a detail mode exposes the steps automatically. Returning to
      // concise preserves the user's temporary disclosure state.
      setOperationsExpanded(true)
    }
    detailContextRef.current = { mode: detailMode, runId: snapshot?.run_id ?? null }
  }, [detailMode, snapshot?.run_id])

  if (!snapshot) {
    if (!loading) return null
    return (
      <section aria-label="Execution status" className="mx-auto w-full max-w-3xl px-4 pt-3">
        <p className="flex items-center gap-2 rounded-xl border border-white/[0.08] bg-white/[0.025] px-3 py-2 text-xs text-neutral-400" role="status" aria-live="polite">
          <Loader2 size={14} className="animate-spin" aria-hidden="true" />
          Loading execution status…
        </p>
      </section>
    )
  }

  // Only pair diagnostics with the selected Run. A stale detail bundle must
  // never appear beside a snapshot after the user switches execution.
  const runOperations = operations.filter((operation) => operation.run_id === snapshot.run_id)
  const runEvents = events.filter((event) => event.run_id === snapshot.run_id)
  const runRecovery = recovery?.run_id === snapshot.run_id && recovery.run.run_id === snapshot.run_id
    ? {
        ...recovery,
        unresolved_operations: recovery.unresolved_operations.filter((operation) => operation.run_id === snapshot.run_id),
      }
    : null
  const verifiedCount = runOperations.filter((operation) => operation.status === 'verified').length
  const availableActions = new Set(snapshot.available_actions)
  const actions = (['pause', 'resume', 'stop'] as const).filter((action) => availableActions.has(action))
  const hasRecovery = snapshot.status === 'interrupted'
    || snapshot.status === 'needs_review'
    || runOperations.some((operation) => operation.status === 'verified'
      && ['file_write', 'file_move', 'file_delete'].includes(operation.operation_kind))
  const runSelector = runs.length > 1 && onSelectRun ? (
    <div className="flex items-center gap-2 text-[11px] text-neutral-500">
      <span>Execution</span>
      <Dropdown
        ariaLabel="Choose execution"
        value={snapshot.run_id}
        options={runs.map((run) => ({ value: run.run_id, label: formatRunChoice(run) }))}
        onChange={onSelectRun}
        size="sm"
        className="max-w-44"
      />
    </div>
  ) : null

  const performAction = async (action: RunAction) => {
    if (!onAction || pendingAction || busy) return
    setPendingAction(action)
    setActionError('')
    try {
      await onAction(action)
    } catch (caught) {
      const message = caught instanceof ApiError && caught.code === 'revision_conflict'
        ? 'The execution changed. The latest status is being loaded.'
        : caught instanceof Error ? caught.message : 'The execution action failed.'
      setActionError(message)
      void onRefresh?.()
    } finally {
      setPendingAction(null)
    }
  }

  return (
    <section
      aria-label="Execution timeline"
      aria-busy={loading || busy || pendingAction !== null}
      className="mx-auto w-full max-w-3xl px-4 pt-3"
    >
      <div className="overflow-hidden rounded-xl border border-white/[0.08] bg-white/[0.025]">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2.5">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <OperationIcon status={snapshot.status === 'completed' ? 'verified' : snapshot.status === 'failed' || snapshot.status === 'cancelled' ? 'failed' : snapshot.status === 'needs_review' || snapshot.status === 'interrupted' ? 'needs_review' : snapshot.status === 'running' || snapshot.status === 'pausing' ? 'executing' : 'prepared'} />
            <div className="min-w-0">
              <p className="text-xs font-medium text-neutral-200" role="status" aria-live="polite">
                {RUN_STATUS_LABELS[snapshot.status]}
              </p>
              <p className="mt-0.5 text-[11px] text-neutral-500">
                {verifiedCount} verified{runOperations.length > 0 ? ` · ${runOperations.length} operation${runOperations.length === 1 ? '' : 's'}` : ''}
              </p>
            </div>
          </div>

          {runSelector}

          <div className="flex items-center gap-1.5">
            {actions.map((action) => (
              <button
                key={action}
                type="button"
                aria-label={`${pendingAction === action ? actionProgressLabel(action) : actionLabel(action)} execution`}
                disabled={Boolean(pendingAction) || busy || !onAction}
                onClick={() => void performAction(action)}
                className={`inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-[11px] font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50 ${action === 'stop' ? 'border-red-400/15 text-red-200 hover:bg-red-400/10' : 'border-white/[0.08] text-neutral-300 hover:bg-white/[0.06]'}`}
              >
                {pendingAction === action ? <Loader2 size={13} className="animate-spin" aria-hidden="true" /> : <ActionIcon action={action} />}
                {pendingAction === action ? actionProgressLabel(action) : actionLabel(action)}
              </button>
            ))}
          </div>
        </div>

        {snapshot.reason_code && (
          <p className="border-t border-white/[0.06] px-3 py-2 text-[11px] text-amber-200" role="status">
            {reasonText(snapshot.reason_code)}
          </p>
        )}
        {detailMode !== 'simple' && (
          <dl aria-label="Execution details" className="grid grid-cols-2 gap-x-4 gap-y-2 border-t border-white/[0.06] px-3 py-2.5 text-[11px] sm:grid-cols-4">
            <div>
              <dt className="text-neutral-500">Attempt</dt>
              <dd className="mt-0.5 text-neutral-300">{snapshot.attempt}</dd>
            </div>
            <div>
              <dt className="text-neutral-500">Started</dt>
              <dd className="mt-0.5 text-neutral-300">{formatRunTime(snapshot.created_at)}</dd>
            </div>
            <div>
              <dt className="text-neutral-500">Last update</dt>
              <dd className="mt-0.5 text-neutral-300">{formatRunTime(snapshot.updated_at)}</dd>
            </div>
            {snapshot.finished_at && (
              <div>
                <dt className="text-neutral-500">Finished</dt>
                <dd className="mt-0.5 text-neutral-300">{formatRunTime(snapshot.finished_at)}</dd>
              </div>
            )}
          </dl>
        )}
        {detailMode === 'developer' && (
          <dl aria-label="Run diagnostics" className="grid grid-cols-2 gap-x-4 gap-y-2 border-t border-white/[0.06] px-3 py-2.5 text-[11px] sm:grid-cols-3">
            <div className="min-w-0">
              <dt className="text-neutral-500">Run ID</dt>
              <dd className="mt-0.5 break-all font-mono text-neutral-300">{snapshot.run_id}</dd>
            </div>
            <div>
              <dt className="text-neutral-500">Revision</dt>
              <dd className="mt-0.5 font-mono text-neutral-300">{snapshot.revision}</dd>
            </div>
            <div>
              <dt className="text-neutral-500">Event cursor</dt>
              <dd className="mt-0.5 font-mono text-neutral-300">{snapshot.last_seq}</dd>
            </div>
            {runEvents.length > 0 && (
              <div className="col-span-full min-w-0">
                <dt className="text-neutral-500">Recent Run events</dt>
                <dd>
                  <ol className="mt-1 space-y-1 font-mono text-neutral-300">
                    {runEvents.slice(-8).map((event) => (
                      <li key={`${event.run_id}:${event.seq}`} className="break-words">
                        #{event.seq} {event.type} · {formatRunTime(event.at)}
                      </li>
                    ))}
                  </ol>
                </dd>
              </div>
            )}
          </dl>
        )}
        {connected === false && snapshot.status === 'running' && (
          <p className="border-t border-white/[0.06] px-3 py-2 text-[11px] text-amber-200" role="status" aria-live="polite">
            Reconnecting to execution updates. The last saved status is shown.
          </p>
        )}
        {(error || actionError) && (
          <div className="flex items-center justify-between gap-3 border-t border-white/[0.06] px-3 py-2 text-[11px] text-red-200" role="alert">
            <span>{actionError || error}</span>
            {onRefresh && (
              <button type="button" onClick={() => void onRefresh()} className="shrink-0 underline underline-offset-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
                Refresh status
              </button>
            )}
          </div>
        )}

        {hasRecovery && (
          <div className="border-t border-white/[0.06] px-3 py-2.5">
            <RecoveryPanel
              snapshot={snapshot}
              operations={runOperations}
              recovery={runRecovery}
              onResolveOperation={onResolveOperation}
              onUndo={onUndo}
              onRefresh={onRefresh}
            />
          </div>
        )}

        {runOperations.length > 0 && (
          <details
            open={operationsExpanded}
            className="border-t border-white/[0.06]"
          >
            <summary
              onClick={(event) => {
                event.preventDefault()
                setOperationsExpanded((expanded) => !expanded)
              }}
              className="cursor-pointer list-none px-3 py-2 text-[11px] font-medium text-neutral-400 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
            >
              Operation details <span className="ml-1 text-neutral-600">({runOperations.length})</span>
            </summary>
            <ol className="space-y-1 px-3 pb-3" aria-label="Execution operations">
              {runOperations.map((operation) => (
                <li key={operation.operation_id} className="flex min-w-0 items-start gap-2 rounded-md bg-black/15 px-2.5 py-2">
                  <span className="mt-0.5 shrink-0"><OperationIcon status={operation.status} /></span>
                  <div className="min-w-0 flex-1">
                    <p className="break-words text-[11px] text-neutral-300">
                      {operation.sequence}. {humanize(operation.operation_kind)}
                      {!browserOperationOutcome(operation.evidence) && <span className="ml-2 text-neutral-500">{OPERATION_STATUS_LABELS[operation.status]}</span>}
                    </p>
                    {detailMode === 'developer' && (
                      <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[10px] text-neutral-500">
                        <dt>Operation ID</dt><dd className="break-all font-mono text-neutral-300">{operation.operation_id}</dd>
                        <dt>Effect class</dt><dd className="text-neutral-300">{operation.effect_class}</dd>
                        <dt>Snapshot</dt><dd className="text-neutral-300">{operation.snapshot_available ? 'Available' : 'Unavailable'}</dd>
                        {operation.reason_code && <><dt>Reason code</dt><dd className="break-all font-mono text-neutral-300">{operation.reason_code}</dd></>}
                      </dl>
                    )}
                    {operation.reason_code && <p className="mt-1 text-[10px] text-neutral-500">{reasonText(operation.reason_code)}</p>}
                    <BrowserVerificationStatus outcome={browserOperationOutcome(operation.evidence)} detail />
                    {Object.keys(operation.evidence).length > 0 && (
                      <details className="mt-1.5">
                        <summary className="cursor-pointer text-[10px] text-neutral-500 underline-offset-2 hover:text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">Verification evidence</summary>
                        <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[10px] text-neutral-500">
                          {Object.entries(operation.evidence).map(([key, value]) => (
                            <div key={key} className="contents">
                              <dt>{humanize(key)}</dt>
                              <dd className="break-all text-neutral-300">{String(value)}</dd>
                            </div>
                          ))}
                        </dl>
                      </details>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </section>
  )
}
