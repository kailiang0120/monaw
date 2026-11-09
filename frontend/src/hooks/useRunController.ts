import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { ApiError } from '../lib/api/client'
import {
  fetchRun,
  fetchRunOperations,
  fetchRunRecovery,
  fetchRuns,
  pauseRun,
  resolveRunOperation,
  resumeRun,
  stopRun,
} from '../lib/api/runs'
import type {
  OperationResolution,
  RecoveryReport,
  RunCommandResponse,
  RunEvent,
  RunOperation,
  RunSnapshot,
} from '../lib/api/runs'
import { useRunEvents } from './useRunEvents'

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const RUN_DISCOVERY_INTERVAL_MS = 2_000

export class RunActionUnavailableError extends Error {
  action: string

  constructor(action: string) {
    super(`The current Run state does not allow ${action}.`)
    this.name = 'RunActionUnavailableError'
    this.action = action
  }
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

function compareRunActivity(left: RunSnapshot, right: RunSnapshot): number {
  const updated = right.updated_at.localeCompare(left.updated_at)
  if (updated !== 0) return updated
  const created = right.created_at.localeCompare(left.created_at)
  if (created !== 0) return created
  return right.run_id.localeCompare(left.run_id)
}

function sameRunSummaries(left: RunSnapshot[], right: RunSnapshot[]): boolean {
  return left.length === right.length && left.every((run, index) => {
    const other = right[index]
    return run.run_id === other.run_id
      && run.revision === other.revision
      && run.last_seq === other.last_seq
      && run.status === other.status
      && run.updated_at === other.updated_at
  })
}

async function fetchAllRuns(conversationId: string | null, signal: AbortSignal): Promise<RunSnapshot[]> {
  const runs: RunSnapshot[] = []
  let afterRunId: string | undefined
  for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
    const page = await fetchRuns(conversationId, { limit: 500, afterRunId, signal })
    runs.push(...page.runs)
    if (!page.has_more || !page.next_run_id || page.next_run_id === afterRunId) break
    afterRunId = page.next_run_id
  }
  return runs.sort(compareRunActivity)
}

async function fetchAllOperations(runId: string, signal: AbortSignal): Promise<RunOperation[]> {
  const operations: RunOperation[] = []
  let afterSequence = 0
  for (let pageIndex = 0; pageIndex < 100; pageIndex += 1) {
    const page = await fetchRunOperations(runId, afterSequence, 500, signal)
    operations.push(...page.operations)
    if (!page.has_more || page.next_sequence <= afterSequence) break
    afterSequence = page.next_sequence
  }
  return operations
}

export interface UseRunControllerResult {
  runs: RunSnapshot[]
  selectedRunId: string | null
  run: RunSnapshot | null
  snapshot: RunSnapshot | null
  operations: RunOperation[]
  recovery: RecoveryReport | null
  events: RunEvent[]
  lastSeq: number
  eventsConnected: boolean
  loading: boolean
  refreshing: boolean
  error: Error | null
  refresh: (options?: { selectLatest?: boolean }) => Promise<void>
  refreshLatest: () => Promise<void>
  refreshRun: () => Promise<void>
  refreshEvents: () => Promise<void>
  selectRun: (runId: string | null) => void
  pause: () => Promise<RunCommandResponse>
  resume: () => Promise<RunCommandResponse>
  stop: () => Promise<RunCommandResponse>
  verifyOperation: (operationId: string) => Promise<unknown>
  skipOperation: (operationId: string) => Promise<unknown>
}

/** Loads the current conversation's Runs and applies actions against server revisions. */
export function useRunController(conversationId: string | null): UseRunControllerResult {
  const [runs, setRuns] = useState<RunSnapshot[]>([])
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null)
  const [operations, setOperations] = useState<RunOperation[]>([])
  const [recovery, setRecovery] = useState<RecoveryReport | null>(null)
  const [listLoading, setListLoading] = useState(false)
  const [runLoading, setRunLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const selectedRunIdRef = useRef<string | null>(null)
  const manuallySelectedRef = useRef(false)
  const snapshotRef = useRef<RunSnapshot | null>(null)
  const snapshotsByRunRef = useRef(new Map<string, RunSnapshot>())
  const revisionFenceByRunRef = useRef(new Map<string, number>())
  const conversationGenerationRef = useRef(0)
  const listRequestRef = useRef(0)
  const detailRequestRef = useRef(0)
  const listAbortRef = useRef<AbortController | null>(null)
  const detailAbortRef = useRef<AbortController | null>(null)

  const setCurrentSnapshot = useCallback((next: RunSnapshot | null): boolean => {
    if (next && selectedRunIdRef.current !== next.run_id) return false
    if (next) {
      const known = snapshotsByRunRef.current.get(next.run_id)
      const knownRevision = Math.max(
        known?.revision ?? -1,
        revisionFenceByRunRef.current.get(next.run_id) ?? -1,
      )
      if (next.revision < knownRevision) return false
      snapshotsByRunRef.current.set(next.run_id, next)
      revisionFenceByRunRef.current.set(next.run_id, next.revision)
    }
    snapshotRef.current = next
    setSnapshot(next)
    return true
  }, [])

  const mergeRunIndex = useCallback((incoming: RunSnapshot[]): RunSnapshot[] => {
    return incoming.map((item) => {
      const known = snapshotsByRunRef.current.get(item.run_id)
      const knownRevision = Math.max(
        known?.revision ?? -1,
        revisionFenceByRunRef.current.get(item.run_id) ?? -1,
      )
      if (knownRevision > item.revision) return known ?? item
      snapshotsByRunRef.current.set(item.run_id, item)
      revisionFenceByRunRef.current.set(item.run_id, item.revision)
      return item
    }).sort(compareRunActivity)
  }, [])

  const loadRunDetails = useCallback(async (runId: string): Promise<void> => {
    const conversationGeneration = conversationGenerationRef.current
    const requestId = ++detailRequestRef.current
    const startingRevision = Math.max(
      snapshotsByRunRef.current.get(runId)?.revision ?? -1,
      revisionFenceByRunRef.current.get(runId) ?? -1,
    )
    detailAbortRef.current?.abort()
    const controller = new AbortController()
    detailAbortRef.current = controller
    setRunLoading(true)
    setError(null)
    try {
      const [runResult, operationsResult, recoveryResult] = await Promise.all([
        fetchRun(runId, controller.signal),
        fetchAllOperations(runId, controller.signal),
        fetchRunRecovery(runId, controller.signal),
      ])
      if (
        controller.signal.aborted
        || conversationGeneration !== conversationGenerationRef.current
        || requestId !== detailRequestRef.current
        || selectedRunIdRef.current !== runId
      ) return
      const latestSnapshot = recoveryResult.run.revision > runResult.revision
        ? recoveryResult.run
        : runResult
      const known = Math.max(
        snapshotsByRunRef.current.get(runId)?.revision ?? -1,
        revisionFenceByRunRef.current.get(runId) ?? -1,
      )
      // A later event, action response, or Run index refresh may have advanced
      // the fence while this request was reading its detail bundle. Drop all
      // of the bundle together so old operations/recovery data cannot pair with
      // or replace a newer snapshot.
      if (known > startingRevision || latestSnapshot.revision < known) return
      if (!setCurrentSnapshot(latestSnapshot)) return
      setOperations(operationsResult)
      setRecovery(recoveryResult)
      setRuns((current) => current.map((item) => item.run_id === runId
        ? { ...item, ...latestSnapshot }
        : item).sort(compareRunActivity))
    } catch (caught) {
      if (!controller.signal.aborted && conversationGeneration === conversationGenerationRef.current
        && requestId === detailRequestRef.current && selectedRunIdRef.current === runId) {
        setError(asError(caught))
      }
      throw caught
    } finally {
      if (conversationGeneration === conversationGenerationRef.current && requestId === detailRequestRef.current) {
        setRunLoading(false)
      }
    }
  }, [setCurrentSnapshot])

  const refreshRun = useCallback(async (): Promise<void> => {
    const runId = selectedRunIdRef.current
    if (!runId) return
    try {
      await loadRunDetails(runId)
    } catch {
      // The error is exposed in state; callers can still decide whether to retry.
    }
  }, [loadRunDetails])

  const refresh = useCallback(async (options: { selectLatest?: boolean } = {}): Promise<void> => {
    const requestId = ++listRequestRef.current
    const conversationGeneration = conversationGenerationRef.current
    listAbortRef.current?.abort()
    const controller = new AbortController()
    listAbortRef.current = controller
    setListLoading(true)
    setError(null)
    try {
      if (!conversationId) {
        if (conversationGeneration === conversationGenerationRef.current
          && requestId === listRequestRef.current) {
          selectedRunIdRef.current = null
          setSelectedRunId(null)
          setRuns([])
          setCurrentSnapshot(null)
          setOperations([])
          setRecovery(null)
          setRunLoading(false)
        }
        return
      }
      const nextRuns = mergeRunIndex(await fetchAllRuns(conversationId, controller.signal))
      if (
        controller.signal.aborted
        || conversationGeneration !== conversationGenerationRef.current
        || requestId !== listRequestRef.current
      ) return
      setRuns(nextRuns)
      if (options.selectLatest) manuallySelectedRef.current = false
      const currentId = options.selectLatest ? null : selectedRunIdRef.current
      const nextId = currentId && nextRuns.some((item) => item.run_id === currentId)
        ? currentId
        : nextRuns[0]?.run_id ?? null
      if (nextId !== currentId) {
        selectedRunIdRef.current = nextId
        setSelectedRunId(nextId)
        setCurrentSnapshot(null)
        setOperations([])
        setRecovery(null)
      }
      if (nextId) {
        const indexSnapshot = nextRuns.find((item) => item.run_id === nextId)
        if (indexSnapshot) setCurrentSnapshot(indexSnapshot)
        await loadRunDetails(nextId)
      } else {
        detailAbortRef.current?.abort()
        detailRequestRef.current += 1
        setCurrentSnapshot(null)
        setOperations([])
        setRecovery(null)
        setRunLoading(false)
      }
    } catch (caught) {
      if (!controller.signal.aborted && conversationGeneration === conversationGenerationRef.current
        && requestId === listRequestRef.current) {
        setError(asError(caught))
      }
    } finally {
      if (conversationGeneration === conversationGenerationRef.current && requestId === listRequestRef.current) {
        setListLoading(false)
      }
    }
  }, [conversationId, loadRunDetails, mergeRunIndex, setCurrentSnapshot])

  const refreshLatest = useCallback(() => refresh({ selectLatest: true }), [refresh])

  useEffect(() => {
    conversationGenerationRef.current += 1
    selectedRunIdRef.current = null
    manuallySelectedRef.current = false
    setSelectedRunId(null)
    setRuns([])
    setCurrentSnapshot(null)
    setOperations([])
    setRecovery(null)
    setError(null)
    listAbortRef.current?.abort()
    detailAbortRef.current?.abort()
    void refresh()
    return () => {
      conversationGenerationRef.current += 1
      listAbortRef.current?.abort()
      detailAbortRef.current?.abort()
    }
  }, [conversationId, refresh, setCurrentSnapshot])

  // Run creation is driven by chat requests, which can begin after the current
  // Run has reached a terminal state. Poll the small local Run index so a new
  // Run is selected without requiring the user to reopen the chat or refresh.
  useEffect(() => {
    if (!conversationId) return
    let active = true
    let inFlight = false
    const controller = new AbortController()
    const discover = async () => {
      if (inFlight || controller.signal.aborted) return
      inFlight = true
      try {
        const nextRuns = mergeRunIndex(await fetchAllRuns(conversationId, controller.signal))
        if (!active || controller.signal.aborted) return
        setRuns((current) => sameRunSummaries(current, nextRuns) ? current : nextRuns)
        const currentId = selectedRunIdRef.current
        let targetId = currentId
        if (!currentId || !nextRuns.some((item) => item.run_id === currentId)) {
          targetId = nextRuns[0]?.run_id ?? null
        } else if (!manuallySelectedRef.current) {
          targetId = nextRuns[0]?.run_id ?? null
        }
        if (targetId !== currentId) {
          selectedRunIdRef.current = targetId
          setSelectedRunId(targetId)
          setCurrentSnapshot(null)
          setOperations([])
          setRecovery(null)
        }
        const indexSnapshot = nextRuns.find((item) => item.run_id === targetId)
        if (indexSnapshot && targetId) setCurrentSnapshot(indexSnapshot)
        const target = nextRuns.find((item) => item.run_id === targetId)
        const selectedSnapshot = snapshotRef.current
        const needsDetails = Boolean(targetId && target && (
          selectedSnapshot?.run_id !== targetId
          || selectedSnapshot.revision !== target.revision
          || selectedSnapshot.last_seq !== target.last_seq
          || selectedSnapshot.updated_at !== target.updated_at
        ))
        if (targetId && needsDetails) {
          await loadRunDetails(targetId)
        }
      } catch {
        // Discovery is best-effort; the explicit refresh path exposes errors.
      } finally {
        inFlight = false
      }
    }
    const timer = window.setInterval(() => { void discover() }, RUN_DISCOVERY_INTERVAL_MS)
    return () => {
      active = false
      controller.abort()
      window.clearInterval(timer)
    }
  }, [conversationId, loadRunDetails, mergeRunIndex, setCurrentSnapshot])

  const selectRun = useCallback((runId: string | null) => {
    if (runId === selectedRunIdRef.current) return
    manuallySelectedRef.current = Boolean(runId)
    selectedRunIdRef.current = runId
    setSelectedRunId(runId)
    setCurrentSnapshot(null)
    setOperations([])
    setRecovery(null)
    setError(null)
    if (runId) {
      void loadRunDetails(runId).catch(() => {})
    } else {
      detailAbortRef.current?.abort()
      detailRequestRef.current += 1
      setRunLoading(false)
    }
  }, [loadRunDetails, setCurrentSnapshot])

  const run = useMemo(
    () => snapshot?.run_id === selectedRunId
      ? snapshot
      : runs.find((item) => item.run_id === selectedRunId) ?? null,
    [runs, selectedRunId, snapshot],
  )
  const startSeq = snapshot?.run_id === selectedRunId
    ? snapshot.last_seq
    : run?.last_seq ?? 0
  const onEventSnapshot = useCallback((next: RunSnapshot) => {
    if (selectedRunIdRef.current !== next.run_id) return
    if (!setCurrentSnapshot(next)) return
    setRuns((current) => mergeRunIndex(current.map((item) => item.run_id === next.run_id
      ? { ...item, ...next }
      : item)))
    void loadRunDetails(next.run_id).catch(() => {})
  }, [loadRunDetails, mergeRunIndex, setCurrentSnapshot])
  const onRunEvent = useCallback((event: RunEvent) => {
    if (selectedRunIdRef.current !== event.run_id) return
    const revision = event.payload?.revision
    if (!Number.isSafeInteger(revision) || typeof revision !== 'number' || revision < 0) return
    const previous = Math.max(
      snapshotsByRunRef.current.get(event.run_id)?.revision ?? -1,
      revisionFenceByRunRef.current.get(event.run_id) ?? -1,
    )
    if (revision <= previous) return
    revisionFenceByRunRef.current.set(event.run_id, revision)
    const current = snapshotRef.current
    if (current?.run_id === event.run_id && current.revision < revision) {
      // The event is authoritative about revision, but not a full snapshot.
      // Hide stale actions until the corresponding snapshot/details reload lands.
      snapshotRef.current = null
      setSnapshot(null)
      setRunLoading(true)
      detailAbortRef.current?.abort()
      detailRequestRef.current += 1
    }
  }, [])
  const { events, lastSeq, connected: eventsConnected, refresh: refreshEvents } = useRunEvents(
    selectedRunId,
    {
      initialSeq: startSeq,
      enabled: Boolean(selectedRunId) && !TERMINAL_STATUSES.has(run?.status ?? ''),
      onEvent: onRunEvent,
      onSnapshot: onEventSnapshot,
    },
  )

  const lastAppliedEventRef = useRef<{ runId: string | null; seq: number }>({ runId: null, seq: 0 })
  useEffect(() => {
    if (lastAppliedEventRef.current.runId !== selectedRunId) {
      lastAppliedEventRef.current = { runId: selectedRunId, seq: startSeq }
    }
    const latestEvent = events
      .filter((event) => event.run_id === selectedRunId)
      .reduce<RunEvent | null>((latest, event) => !latest || event.seq > latest.seq ? event : latest, null)
    if (!selectedRunId || !latestEvent || latestEvent.seq <= lastAppliedEventRef.current.seq) return
    lastAppliedEventRef.current.seq = latestEvent.seq
    const timer = window.setTimeout(() => {
      void loadRunDetails(selectedRunId).catch(() => {})
    }, 100)
    return () => window.clearTimeout(timer)
  }, [events, loadRunDetails, selectedRunId, startSeq])

  const runCommand = useCallback(async (action: 'pause' | 'resume' | 'stop'): Promise<RunCommandResponse> => {
    const current = snapshotRef.current
    const currentRunId = selectedRunIdRef.current
    if (!currentRunId || !current) throw new RunActionUnavailableError(action)
    if ((revisionFenceByRunRef.current.get(currentRunId) ?? current.revision) > current.revision) {
      throw new RunActionUnavailableError('refresh')
    }
    if (!current.available_actions.includes(action)) throw new RunActionUnavailableError(action)
    const send = (snapshot: RunSnapshot) => action === 'pause'
      ? pauseRun(currentRunId, snapshot)
      : action === 'resume'
        ? resumeRun(currentRunId, snapshot)
        : stopRun(currentRunId, snapshot)
    try {
      let response: RunCommandResponse
      try {
        response = await send(current)
      } catch (caught) {
        // A running Run advances its revision with every event, so a Pause or
        // Stop pressed just before the UI caught up would otherwise fail. Both
        // mean the same thing at any revision; Continue is never retried
        // because it acts on the state the user saw.
        if (action === 'resume' || !(caught instanceof ApiError) || caught.code !== 'revision_conflict') throw caught
        await refresh()
        const fresh = snapshotRef.current
        if (selectedRunIdRef.current !== currentRunId || !fresh || fresh.run_id !== currentRunId
          || fresh.revision <= current.revision || !fresh.available_actions.includes(action)) throw caught
        response = await send(fresh)
      }
      if (selectedRunIdRef.current === currentRunId) setCurrentSnapshot(response.run)
      await refresh()
      return response
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) await refresh()
      setError(asError(caught))
      throw caught
    }
  }, [refresh, setCurrentSnapshot])

  const resolveOperation = useCallback(async (
    operationId: string,
    resolution: OperationResolution,
  ): Promise<unknown> => {
    const current = snapshotRef.current
    const currentRunId = selectedRunIdRef.current
    if (!currentRunId || !current || !current.available_actions.includes(resolution)) {
      throw new RunActionUnavailableError(resolution)
    }
    if ((revisionFenceByRunRef.current.get(currentRunId) ?? current.revision) > current.revision) {
      throw new RunActionUnavailableError('refresh')
    }
    try {
      const response = await resolveRunOperation(currentRunId, operationId, resolution, current)
      if (selectedRunIdRef.current === currentRunId) setCurrentSnapshot(response.run)
      await refresh()
      return response
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) await refresh()
      setError(asError(caught))
      throw caught
    }
  }, [refresh, setCurrentSnapshot])

  const actionError = error
  return {
    runs,
    selectedRunId,
    run,
    snapshot,
    operations,
    recovery,
    events,
    lastSeq,
    eventsConnected,
    loading: listLoading || runLoading,
    refreshing: listLoading,
    error: actionError,
    refresh,
    refreshLatest,
    refreshRun,
    refreshEvents,
    selectRun,
    pause: () => runCommand('pause'),
    resume: () => runCommand('resume'),
    stop: () => runCommand('stop'),
    verifyOperation: (operationId) => resolveOperation(operationId, 'verify'),
    skipOperation: (operationId) => resolveOperation(operationId, 'skip'),
  }
}
