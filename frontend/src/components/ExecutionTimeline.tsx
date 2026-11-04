import { useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, Pause, Play, Square } from 'lucide-react'
import type { OperationResolution, RecoveryReport, RunAction as ApiRunAction, RunOperation, RunSnapshot } from '../lib/api/runs'
import type { FileMutationUndoResult, UndoCapability } from '../lib/api/fileMutations'
import { ApiError } from '../lib/api/client'
import { RecoveryPanel } from './RecoveryPanel'

export type RunAction = Extract<ApiRunAction, 'pause' | 'resume' | 'stop'>

export interface ExecutionTimelineProps {
  runs?: RunSnapshot[]
  snapshot?: RunSnapshot | null
  operations?: RunOperation[]
  recovery?: RecoveryReport | null
  loading?: boolean
  error?: string | null
  connected?: boolean
  busy?: boolean
  onSelectRun?: (runId: string) => void
  onAction?: (action: RunAction) => void | Promise<void>
  onResolveOperation?: (operationId: string, resolution: OperationResolution) => void | Promise<void>
  onUndo?: (operation: RunOperation, capability: UndoCapability) => Promise<FileMutationUndoResult>
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

export function ExecutionTimeline({
  runs = [],
  snapshot,
  operations = [],
  recovery,
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

  const verifiedCount = operations.filter((operation) => operation.status === 'verified').length
  const availableActions = new Set(snapshot.available_actions)
  const actions = (['pause', 'resume', 'stop'] as const).filter((action) => availableActions.has(action))
  const hasRecovery = snapshot.status === 'interrupted'
    || snapshot.status === 'needs_review'
    || operations.some((operation) => operation.status === 'verified'
      && ['file_write', 'file_move', 'file_delete'].includes(operation.operation_kind))

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
                {verifiedCount} verified{operations.length > 0 ? ` · ${operations.length} operation${operations.length === 1 ? '' : 's'}` : ''}
              </p>
            </div>
          </div>

          {runs.length > 1 && onSelectRun && (
            <label className="flex items-center gap-2 text-[11px] text-neutral-500">
              <span>Execution</span>
              <select
                aria-label="Choose execution"
                value={snapshot.run_id}
                onChange={(event) => onSelectRun(event.target.value)}
                className="max-w-44 rounded-md border border-white/[0.1] bg-[#171614] px-2 py-1 text-xs text-neutral-300 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
              >
                {runs.map((run) => <option key={run.run_id} value={run.run_id}>{formatRunChoice(run)}</option>)}
              </select>
            </label>
          )}

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
              operations={operations}
              recovery={recovery}
              onResolveOperation={onResolveOperation}
              onUndo={onUndo}
              onRefresh={onRefresh}
            />
          </div>
        )}

        {operations.length > 0 && (
          <details className="border-t border-white/[0.06]">
            <summary className="cursor-pointer list-none px-3 py-2 text-[11px] font-medium text-neutral-400 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40">
              Operation details <span className="ml-1 text-neutral-600">({operations.length})</span>
            </summary>
            <ol className="space-y-1 px-3 pb-3" aria-label="Execution operations">
              {operations.map((operation) => (
                <li key={operation.operation_id} className="flex min-w-0 items-start gap-2 rounded-md bg-black/15 px-2.5 py-2">
                  <span className="mt-0.5 shrink-0"><OperationIcon status={operation.status} /></span>
                  <div className="min-w-0 flex-1">
                    <p className="break-words text-[11px] text-neutral-300">
                      {operation.sequence}. {humanize(operation.operation_kind)}
                      <span className="ml-2 text-neutral-500">{OPERATION_STATUS_LABELS[operation.status]}</span>
                    </p>
                    {operation.reason_code && <p className="mt-1 text-[10px] text-neutral-500">{reasonText(operation.reason_code)}</p>}
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
