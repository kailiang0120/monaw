import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Clock3, Loader2, RotateCcw, ShieldAlert } from 'lucide-react'
import { ApiError } from '../lib/api/client'
import { fetchUndoCapability, previewUndoFileMutation } from '../lib/api/fileMutations'
import type { FileMutationUndoResult, UndoCapability, UndoPreview } from '../lib/api/fileMutations'
import type { OperationResolution, RecoveryReport, RunOperation, RunSnapshot } from '../lib/api/runs'
import { UnifiedDiff } from './UnifiedDiff'

type CapabilityStatus = UndoCapability['status']

const UNRESOLVED_STATUSES = new Set<RunOperation['status']>([
  'prepared', 'authorized', 'executing', 'uncertain', 'needs_review',
])

const CAPABILITY_COPY: Record<CapabilityStatus, string> = {
  available: 'Undo is available',
  unavailable: 'Undo is unavailable for this operation',
  expired: 'Undo evidence has expired',
  undone: 'This change has already been undone',
  needs_review: 'This operation needs review before Undo',
  conflict: 'The file changed after this operation. Undo is unavailable.',
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (letter) => letter.toUpperCase())
}

function undoFileState(state: Record<string, unknown>): string {
  if (state.exists === false) return 'Will not exist'
  const kind = typeof state.kind === 'string' ? humanize(state.kind) : 'File'
  const size = typeof state.size === 'number' && Number.isFinite(state.size)
    ? ` · ${state.size.toLocaleString()} bytes`
    : ''
  return `${kind}${size}`
}

function resolutionErrorText(error: unknown, resolution: OperationResolution): string {
  if (error instanceof ApiError && error.code === 'revision_conflict') {
    return 'The execution changed. The latest status is being loaded.'
  }
  return error instanceof Error ? error.message : `Could not ${resolution} this operation.`
}

function undoErrorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'undo_conflict') return 'File changed after this operation. Undo was not applied.'
    if (error.code === 'permission_denied') return 'Current file permissions do not allow Undo.'
    if (error.code === 'undo_confirmation_expired') return 'The Undo preview expired. Prepare a new preview before confirming.'
    if (error.code === 'revision_conflict' || error.code === 'stale_preview') {
      return 'The execution changed before Undo. The latest status is being loaded.'
    }
    if (error.code === 'undo_unavailable') return 'Undo is no longer available for this operation.'
  }
  return error instanceof Error ? error.message : 'Undo could not be completed.'
}

export interface RecoveryPanelProps {
  snapshot: RunSnapshot
  operations?: RunOperation[]
  recovery?: RecoveryReport | null
  onResolveOperation?: (operationId: string, resolution: OperationResolution) => void | Promise<void>
  onUndo?: (operation: RunOperation, capability: UndoCapability, confirmationId: string) => Promise<FileMutationUndoResult>
  onRefresh?: () => void | Promise<void>
}

export function RecoveryPanel({
  snapshot,
  operations = [],
  recovery,
  onResolveOperation,
  onUndo,
  onRefresh,
}: RecoveryPanelProps) {
  const [undoCapabilities, setUndoCapabilities] = useState<Record<string, UndoCapability>>({})
  const [undoCheckErrors, setUndoCheckErrors] = useState<Record<string, string>>({})
  const [undoOutcomes, setUndoOutcomes] = useState<Record<string, string>>({})
  const [pendingResolution, setPendingResolution] = useState<{ id: string; action: OperationResolution } | null>(null)
  const [pendingUndoId, setPendingUndoId] = useState<string | null>(null)
  const [undoPreviews, setUndoPreviews] = useState<Record<string, UndoPreview>>({})
  const [pendingUndoPreviewId, setPendingUndoPreviewId] = useState<string | null>(null)
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({})
  const undoPreviewRequestRef = useRef<{ operationId: string; contextKey: string; controller: AbortController } | null>(null)
  const undoContextKey = `${snapshot.run_id}\0${snapshot.conversation_id}\0${snapshot.revision}`
  const undoContextKeyRef = useRef(undoContextKey)
  undoContextKeyRef.current = undoContextKey

  const visibleOperations = useMemo(() => {
    const byId = new Map(operations.map((operation) => [operation.operation_id, operation]))
    for (const operation of recovery?.unresolved_operations ?? []) {
      if (!byId.has(operation.operation_id)) byId.set(operation.operation_id, operation)
    }
    return [...byId.values()].sort((left, right) => left.sequence - right.sequence)
  }, [operations, recovery?.unresolved_operations])

  const undoCandidates = visibleOperations.filter((operation) => (
    operation.status === 'verified'
    && operation.effect_class === 'verifiable_write'
    && ['file_write', 'file_move', 'file_delete'].includes(operation.operation_kind)
  ))
  const candidateKey = JSON.stringify(undoCandidates.map((operation) => operation.operation_id))
  const unresolved = visibleOperations.filter((operation) => UNRESOLVED_STATUSES.has(operation.status))

  useEffect(() => {
    undoPreviewRequestRef.current?.controller.abort()
    undoPreviewRequestRef.current = null
    const operationIds = JSON.parse(candidateKey) as string[]
    const controller = new AbortController()
    let cancelled = false
    setUndoCapabilities({})
    setUndoCheckErrors({})
    setUndoPreviews({})
    setPendingUndoPreviewId(null)
    if (operationIds.length === 0) return () => controller.abort()

    void Promise.all(operationIds.map(async (operationId) => {
      try {
        const capability = await fetchUndoCapability(
          operationId,
          snapshot.run_id,
          snapshot.conversation_id,
          controller.signal,
        )
        if (cancelled) return
        setUndoCapabilities((current) => ({ ...current, [operationId]: capability }))
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return
        setUndoCheckErrors((current) => ({
          ...current,
          [operationId]: 'Undo status could not be checked. Refresh to try again.',
        }))
      }
    }))

    return () => {
      cancelled = true
      controller.abort()
      if (undoPreviewRequestRef.current?.contextKey === undoContextKey) {
        undoPreviewRequestRef.current.controller.abort()
        undoPreviewRequestRef.current = null
      }
    }
  }, [candidateKey, undoContextKey])

  const runResolution = async (operationId: string, resolution: OperationResolution) => {
    if (!onResolveOperation || pendingResolution || pendingUndoId || pendingUndoPreviewId) return
    setPendingResolution({ id: operationId, action: resolution })
    setActionErrors((current) => ({ ...current, [operationId]: '' }))
    try {
      await onResolveOperation(operationId, resolution)
      void onRefresh?.()
    } catch (error) {
      setActionErrors((current) => ({ ...current, [operationId]: resolutionErrorText(error, resolution) }))
      void onRefresh?.()
    } finally {
      setPendingResolution(null)
    }
  }

  const runUndo = async (operation: RunOperation) => {
    const capability = undoCapabilities[operation.operation_id]
    const preview = undoPreviews[operation.operation_id]
    if (!onUndo || !capability || capability.status !== 'available' || !preview || pendingUndoId || pendingUndoPreviewId) return
    setPendingUndoId(operation.operation_id)
    setActionErrors((current) => ({ ...current, [operation.operation_id]: '' }))
    try {
      const result = await onUndo(operation, capability, preview.confirmation_id)
      setUndoPreviews((current) => {
        const next = { ...current }
        delete next[operation.operation_id]
        return next
      })
      if (result.status === 'undone') {
        setUndoCapabilities((current) => ({
          ...current,
          [operation.operation_id]: { ...capability, status: 'undone' },
        }))
        setUndoOutcomes((current) => ({ ...current, [operation.operation_id]: '' }))
      } else {
        setUndoOutcomes((current) => ({ ...current, [operation.operation_id]: result.status || 'unknown' }))
        if (result.status === 'needs_review') {
          setUndoCapabilities((current) => ({
            ...current,
            [operation.operation_id]: { ...capability, status: 'needs_review', reason_code: result.reason_code },
          }))
        }
      }
      void onRefresh?.()
    } catch (error) {
      setActionErrors((current) => ({ ...current, [operation.operation_id]: undoErrorText(error) }))
      if (error instanceof ApiError) {
        const status: CapabilityStatus | undefined = error.code === 'undo_conflict' ? 'conflict'
          : error.code === 'undo_unavailable' ? 'unavailable'
            : error.code === 'external_effect_unknown' ? 'needs_review' : undefined
        if (status) {
          setUndoCapabilities((current) => ({
            ...current,
            [operation.operation_id]: { ...capability, status },
          }))
        }
      }
      void onRefresh?.()
    } finally {
      setPendingUndoId(null)
    }
  }

  const prepareUndoPreview = async (operation: RunOperation) => {
    const capability = undoCapabilities[operation.operation_id]
    if (
      !capability
      || capability.status !== 'available'
      || pendingUndoPreviewId
      || pendingUndoId
      || Object.keys(undoPreviews).length > 0
    ) return
    setPendingUndoPreviewId(operation.operation_id)
    setActionErrors((current) => ({ ...current, [operation.operation_id]: '' }))
    const requestContextKey = undoContextKey
    const controller = new AbortController()
    undoPreviewRequestRef.current?.controller.abort()
    undoPreviewRequestRef.current = {
      operationId: operation.operation_id,
      contextKey: requestContextKey,
      controller,
    }
    try {
      const preview = await previewUndoFileMutation(operation.operation_id, {
        run_id: snapshot.run_id,
        conversation_id: snapshot.conversation_id,
        expected_revision: capability.revision,
      }, controller.signal)
      if (
        controller.signal.aborted
        || undoContextKeyRef.current !== requestContextKey
        || undoPreviewRequestRef.current?.controller !== controller
      ) return
      setUndoPreviews((current) => ({ ...current, [operation.operation_id]: preview }))
    } catch (error) {
      if (
        controller.signal.aborted
        || undoContextKeyRef.current !== requestContextKey
        || (error instanceof DOMException && error.name === 'AbortError')
      ) return
      setActionErrors((current) => ({ ...current, [operation.operation_id]: undoErrorText(error) }))
      if (error instanceof ApiError && error.code === 'undo_conflict') {
        setUndoCapabilities((current) => ({
          ...current,
          [operation.operation_id]: { ...capability, status: 'conflict', reason_code: 'undo_conflict' },
        }))
      }
      void onRefresh?.()
    } finally {
      if (undoPreviewRequestRef.current?.controller === controller) {
        undoPreviewRequestRef.current = null
        setPendingUndoPreviewId(null)
      }
    }
  }

  const cancelUndoPreview = (operationId: string) => {
    setUndoPreviews((current) => {
      const next = { ...current }
      delete next[operationId]
      return next
    })
  }

  const refreshUndoStatus = async (operation: RunOperation) => {
    setUndoCheckErrors((current) => ({ ...current, [operation.operation_id]: '' }))
    try {
      const capability = await fetchUndoCapability(
        operation.operation_id,
        snapshot.run_id,
        snapshot.conversation_id,
      )
      setUndoCapabilities((current) => ({ ...current, [operation.operation_id]: capability }))
      setUndoOutcomes((current) => ({ ...current, [operation.operation_id]: '' }))
      setActionErrors((current) => ({ ...current, [operation.operation_id]: '' }))
      void onRefresh?.()
    } catch {
      setUndoCheckErrors((current) => ({
        ...current,
        [operation.operation_id]: 'Undo status could not be checked. Refresh to try again.',
      }))
    }
  }

  if (unresolved.length === 0 && undoCandidates.length === 0) return null

  const needsReview = snapshot.status === 'interrupted' || snapshot.status === 'needs_review'
  return (
    <section aria-label="Recovery and file changes" className="space-y-2">
      <header className="flex items-center gap-2">
        {needsReview
          ? <ShieldAlert size={14} className="shrink-0 text-amber-300" aria-hidden="true" />
          : <RotateCcw size={14} className="shrink-0 text-neutral-400" aria-hidden="true" />}
        <h2 className="text-xs font-medium text-neutral-200">{needsReview ? 'Review before continuing' : 'File changes'}</h2>
      </header>

      {snapshot.status === 'interrupted' && (
        <p className="text-[11px] leading-relaxed text-amber-200" role="status">
          This execution was interrupted. Check any action that may have taken effect before continuing.
        </p>
      )}
      {needsReview && (
        <p className="text-[11px] leading-relaxed text-amber-200" role="status">
          The execution stopped with operations that need attention. An operation marked uncertain may have taken effect; unverified results will not be repeated automatically.
        </p>
      )}

      <ul className="space-y-2">
        {visibleOperations.map((operation) => {
          const actions = new Set(snapshot.available_actions)
          const canVerify = actions.has('verify')
            && operation.effect_class === 'verifiable_write'
            && operation.snapshot_available
            && operation.attempt === snapshot.attempt
            && ['executing', 'uncertain', 'needs_review'].includes(operation.status)
          const canSkip = actions.has('skip') && UNRESOLVED_STATUSES.has(operation.status)
          const capability = undoCapabilities[operation.operation_id]
          const undoPreview = undoPreviews[operation.operation_id]
          const hasOtherUndoPreview = Object.keys(undoPreviews).some((operationId) => operationId !== operation.operation_id)
          const isUnresolved = UNRESOLVED_STATUSES.has(operation.status)
          const isCandidate = operation.status === 'verified'
            && operation.effect_class === 'verifiable_write'
            && ['file_write', 'file_move', 'file_delete'].includes(operation.operation_kind)

          if (!isUnresolved && !isCandidate) return null

          return (
            <li key={operation.operation_id} className="rounded-lg border border-white/[0.06] bg-black/15 px-2.5 py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  {isUnresolved
                    ? <AlertTriangle size={13} className="shrink-0 text-amber-300" aria-hidden="true" />
                    : <CheckCircle2 size={13} className="shrink-0 text-emerald-300" aria-hidden="true" />}
                  <div className="min-w-0">
                    <p className="break-words text-[11px] text-neutral-300">
                      {humanize(operation.operation_kind)} <span className="text-neutral-500">· {humanize(operation.status)}</span>
                    </p>
                    {typeof operation.input_summary.path === 'string' && <p className="mt-1 break-all font-mono text-[10px] text-neutral-400">{operation.input_summary.path}</p>}
                    {operation.reason_code && operation.reason_code !== operation.status && <p className="mt-1 break-words text-[10px] text-neutral-500">{humanize(operation.reason_code)}</p>}
                    {operation.effect_class === 'opaque_effect' && isUnresolved && (
                      <p className="mt-1 text-[10px] leading-relaxed text-amber-200">This action can’t be checked automatically. Review its effect before continuing.</p>
                    )}
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-1.5">
                  {canVerify && onResolveOperation && (
                    <button
                      type="button"
                      disabled={pendingResolution !== null || pendingUndoId !== null}
                      onClick={() => void runResolution(operation.operation_id, 'verify')}
                      className="inline-flex items-center gap-1 rounded-md border border-white/[0.08] px-2 py-1 text-[10px] font-medium text-neutral-300 hover:bg-white/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {pendingResolution?.id === operation.operation_id && pendingResolution.action === 'verify'
                        ? <Loader2 size={11} className="animate-spin" aria-hidden="true" /> : <CheckCircle2 size={11} aria-hidden="true" />}
                      Verify
                    </button>
                  )}
                  {canSkip && onResolveOperation && (
                    <button
                      type="button"
                      disabled={pendingResolution !== null || pendingUndoId !== null}
                      onClick={() => void runResolution(operation.operation_id, 'skip')}
                      className="rounded-md border border-white/[0.08] px-2 py-1 text-[10px] font-medium text-neutral-300 hover:bg-white/[0.06] focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Skip
                    </button>
                  )}
                  {isCandidate && (
                    undoOutcomes[operation.operation_id]
                      ? <div className="flex items-center gap-2">
                          <span className="text-[10px] text-amber-200" role="status">
                            Undo returned {humanize(undoOutcomes[operation.operation_id])}. Check the latest status before retrying.
                          </span>
                          <button
                            type="button"
                            onClick={() => void refreshUndoStatus(operation)}
                            className="shrink-0 text-[10px] text-neutral-400 underline underline-offset-2 hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
                          >
                            Refresh
                          </button>
                        </div>
                      : capability?.status === 'available' && onUndo
                      ? pendingUndoId === operation.operation_id
                        ? <span className="inline-flex items-center gap-1 text-[10px] text-amber-200" role="status"><Loader2 size={11} className="animate-spin" aria-hidden="true" />Undoing…</span>
                        : pendingUndoPreviewId === operation.operation_id
                        ? <span className="inline-flex items-center gap-1 text-[10px] text-amber-200" role="status"><Loader2 size={11} className="animate-spin" aria-hidden="true" />Preparing Undo preview…</span>
                        : undoPreview
                        ? <span className="text-[10px] text-amber-200" role="status">Review the changes below</span>
                        : <button
                            type="button"
                            disabled={pendingUndoId !== null || pendingUndoPreviewId !== null || pendingResolution !== null || hasOtherUndoPreview}
                            onClick={() => void prepareUndoPreview(operation)}
                            className="inline-flex items-center gap-1 rounded-md border border-red-400/20 px-2 py-1 text-[10px] font-medium text-red-200 hover:bg-red-400/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400/40 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            <RotateCcw size={11} aria-hidden="true" /> Undo
                          </button>
                      : capability
                        ? <span className={`text-[10px] ${capability.status === 'conflict' ? 'text-amber-200' : 'text-neutral-500'}`} role={capability.status === 'conflict' ? 'status' : undefined}>
                            {CAPABILITY_COPY[capability.status]}
                          </span>
                        : undoCheckErrors[operation.operation_id]
                          ? <span className="text-[10px] text-amber-200" role="status">{undoCheckErrors[operation.operation_id]}</span>
                          : <span className="inline-flex items-center gap-1 text-[10px] text-neutral-500" role="status"><Clock3 size={11} aria-hidden="true" />Checking Undo…</span>
                  )}
                </div>
              </div>
              {undoPreview && capability?.status === 'available' && onUndo && (
                <section
                  aria-label="Undo confirmation preview"
                  className="mt-2 space-y-2 rounded-md border border-red-400/20 bg-red-400/[0.04] p-2.5"
                  role="group"
                >
                  <div>
                    <h3 className="text-[10px] font-medium text-neutral-200">Review what Undo will change</h3>
                    <p className="mt-1 text-[10px] leading-relaxed text-neutral-400">
                      This preview is bound to the current file state and expires in {Math.max(0, Math.ceil(undoPreview.expires_in_seconds / 60))} minutes.
                    </p>
                  </div>
                  <ul className="space-y-1.5" aria-label="Files affected by Undo">
                    {undoPreview.files.map((file) => {
                      const diff = file.preview
                      const diffText = typeof diff.text === 'string' ? diff.text : ''
                      return (
                        <li key={`${file.path}-${file.role}`} className="rounded border border-white/[0.06] bg-black/20 p-2">
                          <p className="break-all font-mono text-[10px] text-neutral-300">{file.path}</p>
                          <p className="mt-0.5 text-[10px] text-neutral-500">Undo will {humanize(file.action)} this file.</p>
                          <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-2 gap-y-1 text-[10px]">
                            <dt className="text-neutral-600">Current</dt>
                            <dd className="text-neutral-400">{undoFileState(file.before)}</dd>
                            <dt className="text-neutral-600">After Undo</dt>
                            <dd className="text-neutral-400">{undoFileState(file.after)}</dd>
                          </dl>
                          {diff.kind === 'unified_diff' && diffText
                            ? <UnifiedDiff label={`Undo text diff for ${file.path}`} text={diffText} className="mt-2 max-h-48" />
                            : <p className="mt-2 text-[10px] text-neutral-500">A text diff is unavailable for this file; the byte sizes above show the change.</p>}
                        </li>
                      )
                    })}
                  </ul>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      disabled={pendingUndoId !== null || pendingResolution !== null}
                      onClick={() => void runUndo(operation)}
                      className="rounded-md border border-red-400/30 bg-red-400/10 px-2.5 py-1.5 text-[10px] font-medium text-red-200 hover:bg-red-400/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400/40 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Undo
                    </button>
                    <button
                      type="button"
                      autoFocus
                      disabled={pendingUndoId !== null}
                      onClick={() => cancelUndoPreview(operation.operation_id)}
                      className="rounded-md px-2.5 py-1.5 text-[10px] text-neutral-400 hover:bg-white/[0.06] hover:text-neutral-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </section>
              )}
              {actionErrors[operation.operation_id] && (
                <p className="mt-2 text-[10px] text-amber-200" role="alert">{actionErrors[operation.operation_id]}</p>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
