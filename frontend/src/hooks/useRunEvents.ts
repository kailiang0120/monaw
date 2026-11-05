import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../lib/api/client'
import {
  fetchRun,
  fetchRunEvents,
  RunEventStreamResetError,
  streamRunEvents,
} from '../lib/api/runs'
import type { RunEvent, RunSnapshot } from '../lib/api/runs'

const EVENT_PAGE_SIZE = 500
const MAX_RECONNECT_DELAY_MS = 5_000

export interface UseRunEventsOptions {
  initialSeq?: number
  enabled?: boolean
  onEvent?: (event: RunEvent) => void
  onSnapshot?: (snapshot: RunSnapshot) => void
}

export interface UseRunEventsResult {
  events: RunEvent[]
  lastSeq: number
  connected: boolean
  error: Error | null
  refresh: () => Promise<void>
}

function isEventsExpired(error: unknown): boolean {
  return error instanceof ApiError
    ? error.status === 410 || error.code === 'events_expired'
    : error instanceof RunEventStreamResetError && error.code === 'events_expired'
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

function waitForReconnect(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.resolve()
  return new Promise((resolve) => {
    const finish = () => {
      window.clearTimeout(timer)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    const timer = window.setTimeout(finish, delayMs)
    signal.addEventListener('abort', finish, { once: true })
  })
}

/** Replay durable events before following SSE, and keep the cursor scoped to one Run. */
export function useRunEvents(
  runId: string | null,
  options: UseRunEventsOptions = {},
): UseRunEventsResult {
  const requestedSeq = options.initialSeq ?? 0
  const initialSeq = Number.isSafeInteger(requestedSeq) && requestedSeq >= 0 ? requestedSeq : 0
  const enabled = options.enabled ?? true
  const onEventRef = useRef(options.onEvent)
  const onSnapshotRef = useRef(options.onSnapshot)
  const refreshRef = useRef<(() => Promise<void>) | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const [lastSeq, setLastSeq] = useState(initialSeq)
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    onEventRef.current = options.onEvent
  }, [options.onEvent])

  useEffect(() => {
    onSnapshotRef.current = options.onSnapshot
  }, [options.onSnapshot])

  useEffect(() => {
    setEvents([])
    setLastSeq(initialSeq)
    setConnected(false)
    setError(null)
    refreshRef.current = null

    if (!runId || !enabled) return

    let active = true
    let cursor = initialSeq
    let retries = 0
    let pageSync: Promise<void> | null = null
    const controller = new AbortController()
    const bySequence = new Map<number, RunEvent>()

    const publish = (event: RunEvent) => {
      if (!active || event.run_id !== runId || !Number.isInteger(event.seq) || event.seq < 1) return
      if (event.seq <= cursor) return
      cursor = event.seq
      bySequence.set(event.seq, event)
      onEventRef.current?.(event)
      setEvents([...bySequence.values()].sort((left, right) => left.seq - right.seq))
      setLastSeq(cursor)
    }

    const recoverExpiredCursor = async () => {
      const snapshot = await fetchRun(runId, controller.signal)
      if (!active) return
      // We no longer have a complete event history. Start from the snapshot's
      // atomic cursor and let the caller replace its state from that snapshot.
      bySequence.clear()
      cursor = snapshot.last_seq
      setEvents([])
      setLastSeq(cursor)
      setError(null)
      onSnapshotRef.current?.(snapshot)
    }

    const syncPages = (): Promise<void> => {
      if (pageSync) return pageSync
      const task = (async () => {
        while (active && !controller.signal.aborted) {
          const pageStart = cursor
          let page
          try {
            page = await fetchRunEvents(runId, cursor, EVENT_PAGE_SIZE, controller.signal)
          } catch (caught) {
            if (isEventsExpired(caught)) {
              await recoverExpiredCursor()
              return
            }
            throw caught
          }
          if (!active) return
          for (const event of page.events) {
            if (event.seq > cursor + 1) {
              await recoverExpiredCursor()
              return
            }
            publish(event)
          }
          const next = Number.isInteger(page.next_seq) ? page.next_seq : cursor
          if (next <= pageStart || page.events.length < EVENT_PAGE_SIZE) return
        }
      })()
      pageSync = task.finally(() => {
        pageSync = null
      })
      return pageSync
    }

    const handleStreamEvent = async (event: RunEvent) => {
      if (!active || event.run_id !== runId || !Number.isInteger(event.seq) || event.seq < 1) return
      if (event.seq > cursor + 1) await syncPages()
      if (event.seq > cursor + 1) {
        // A retention gap can open while the SSE connection is alive.
        await recoverExpiredCursor()
        return
      }
      publish(event)
    }

    refreshRef.current = syncPages

    const run = async () => {
      while (active && !controller.signal.aborted) {
        try {
          await syncPages()
          await streamRunEvents(runId, cursor, handleStreamEvent, {
            signal: controller.signal,
            onOpen: () => {
              if (active) {
                retries = 0
                setConnected(true)
                setError(null)
              }
            },
          })
          if (!active || controller.signal.aborted) break
          setConnected(false)
          // A normal server close means the Run reached a terminal state. If
          // it is still active, reconnect and bridge the close/reconnect gap.
          const snapshot = await fetchRun(runId, controller.signal)
          if (!active) break
          onSnapshotRef.current?.(snapshot)
          if (['completed', 'failed', 'cancelled'].includes(snapshot.status)) break
        } catch (caught) {
          if (!active || controller.signal.aborted) break
          setConnected(false)
          if (isEventsExpired(caught)) {
            try {
              await recoverExpiredCursor()
              retries = 0
            } catch (refreshError) {
              setError(asError(refreshError))
            }
          } else if (caught instanceof RunEventStreamResetError && caught.code === 'auth_expired') {
            // apiFetch refreshes the bearer token before the next connection.
            setError(null)
          } else {
            setError(asError(caught))
          }
        }
        if (!active || controller.signal.aborted) break
        const delay = Math.min(250 * (2 ** Math.min(retries, 5)), MAX_RECONNECT_DELAY_MS)
        retries += 1
        await waitForReconnect(delay, controller.signal)
      }
      if (active) setConnected(false)
    }

    void run()
    return () => {
      active = false
      controller.abort()
      setConnected(false)
      if (refreshRef.current === syncPages) refreshRef.current = null
    }
  // initialSeq is sampled when the run subscription is opened. Snapshot refreshes
  // for that same Run must not reset an active cursor.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, enabled])

  const refresh = useCallback(async () => {
    await refreshRef.current?.()
  }, [])

  return { events, lastSeq, connected, error, refresh }
}
